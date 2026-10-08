"""
The runtime platform over the running Trade-Bot process.

``production_discoveries`` reads every runtime object the process holds,
through the read-only views those objects expose:

* strategies, with their kill-switch state;
* the self-tuning switch and every registered tuning parameter;
* per timeframe, the live model and any shadow candidate (signal engines);
* the Crypto-Box ensemble -- E-01..E-18, consensus, risk quantifier, signal
  gate -- and the data providers that fill its inputs;
* the orchestrator's loops and in-flight retrains, and the API's own tasks;
* the orchestrator's thread pools and, with INTEL_ENABLED, the horizon
  worker pool and the MLflow-registered upgrade artifacts;
* the event bus.

with the dependency edges the code really has (each one cites where it is
read). ``production_controllers`` wires the three controllers whose
subsystem genuinely offers the operation (controllers.py); every other
component is observe-only and says so in its capabilities.

``RuntimeLoop`` keeps the registry true and the desired state enforced:
every pass it re-reads the discoveries (new components registered, changes
observed, components their owner dropped retired), applies stored intents
that were waiting for their component, and runs one reconcile pass through
the change manager -- risk-reducing steps execute, everything else waits for
an approver. It runs on the event loop between ticks and never awaits a
subsystem; the only I/O it starts (persistence flush, the MLflow listing)
is awaited off the trading path.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog

from src.eventbus import EventBus
from src.runtime.adapters import (
    ENSEMBLE,
    SELF_TUNING,
    Discovery,
    ensemble_discoveries,
    eventbus_discovery,
    live_model_id,
    provider_task_discoveries,
    self_tuning_discovery,
    signal_model_discoveries,
    strategy_discoveries,
    task_discoveries,
    tuning_discoveries,
    upgrade_row_discoveries,
    worker_discovery,
    worker_pool_discoveries,
)
from src.runtime.contracts import (
    ComponentRecord,
    ComponentType,
    DependencyDescriptor,
    HealthReport,
    HealthState,
    LifecycleState,
    RuntimeContractError,
    make_component_id,
)
from src.runtime.controllers import (
    RoutingController,
    SelfTuningController,
    ShadowCandidateController,
    StrategyKillSwitchController,
)
from src.runtime.platform import RuntimePlatform
from src.runtime.reconcile import ReconcileOutcome
from src.runtime.supervisor import Controller

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

EVENTBUS = make_component_id(ComponentType.EVENTBUS, "main")
SIGNAL_GATE = make_component_id(ComponentType.ENGINE, "signal_gate")

# Provider-cache field -> the component that fills it (ProviderCache.set_*):
# the Crypto-Box provider loops (Orchestrator._crypto_box_provider_tasks,
# keyed by the same field) and the orderbook stream (OrderbookStream ->
# set_orderbook). ``onchain`` has no producer anywhere in the tree, so E-05's
# edge to it is deliberately absent.
CACHE_FIELD_PRODUCERS: dict[str, str] = {
    "sentiment": make_component_id(ComponentType.PROVIDER, "sentiment"),
    "macro": make_component_id(ComponentType.PROVIDER, "macro"),
    "options": make_component_id(ComponentType.PROVIDER, "options"),
    "exchange_flows": make_component_id(ComponentType.PROVIDER, "exchange_flows"),
    "block_height": make_component_id(ComponentType.PROVIDER, "block_height"),
    "orderbook": make_component_id(ComponentType.TASK, "orderbook_stream"),
}

# Tasks of the API process that consume the bus through their own
# subscription (src/api/main.py lifespan).
API_TASK_DEPENDENCIES: dict[str, tuple[DependencyDescriptor, ...]] = {
    "ws_fanout": (DependencyDescriptor(EVENTBUS),),
    "decision_traces": (DependencyDescriptor(EVENTBUS),),
}


class _OrchestratorView(Protocol):
    """The read-only views src.engine.orchestrator.Orchestrator exposes."""

    @property
    def crypto_box(self) -> Any: ...

    def signal_engines(self) -> dict[str, Any]: ...

    def background_tasks(self) -> tuple[asyncio.Task[Any], ...]: ...

    def provider_tasks(self) -> dict[str, tuple[asyncio.Task[Any], ...]]: ...

    def worker_pools(self) -> dict[str, dict[str, Any]]: ...


class _PauseSwitch(Protocol):
    @property
    def paused(self) -> bool: ...

    def set_paused_now(self, value: bool) -> None: ...


def _utc_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class ProductionSources:
    """Everything the running process holds, as the runtime platform reads it."""

    bus: EventBus
    strategies: Any  # src.strategies.registry.StrategyRegistry
    kill_switch: Any  # src.risk.strategy_kill_switch.StrategyKillSwitchManager
    parameters: Any  # src.tuning.registry.ParameterRegistry
    pause: _PauseSwitch  # src.tuning.state.pause_state
    on_pause_change: Callable[[bool], None]
    orchestrator: Callable[[], _OrchestratorView | None]
    scheduler_running: Callable[[], bool]
    api_tasks: Callable[[], Iterable[asyncio.Task[Any]]]
    intel: Callable[[], Any | None] = lambda: None  # src.intel.CryptoIntelligence
    clock_ms: Callable[[], int] = _utc_ms
    # The MLflow listing, refreshed off the event loop (UpgradeListing).
    upgrade_rows: list[dict[str, Any]] = field(default_factory=list)


def _impl(obj: object) -> str:
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


def _orchestrator_task_dependencies(
    tasks: Iterable[asyncio.Task[Any]], timeframes: Iterable[str], ensemble_on: bool
) -> dict[str, tuple[DependencyDescriptor, ...]]:
    """
    A timeframe loop (``loop_<tf>``) ticks that timeframe's signal engine,
    which predicts with its live model; with Crypto-Box on, the tick also
    asks the ensemble (optional: it fails open).
    """
    names = {t.get_name() for t in tasks}
    deps: dict[str, tuple[DependencyDescriptor, ...]] = {}
    for tf in timeframes:
        name = f"loop_{tf}"
        if name not in names:
            continue
        edges = [DependencyDescriptor(live_model_id(tf))]
        if ensemble_on:
            edges.append(
                DependencyDescriptor(
                    make_component_id(ComponentType.ENGINE, ENSEMBLE), required=False
                )
            )
        deps[name] = tuple(edges)
    return deps


def production_discoveries(sources: ProductionSources) -> list[Discovery]:
    rows: list[Discovery] = [
        eventbus_discovery(sources.bus),
        *strategy_discoveries(sources.strategies, sources.kill_switch, quarantinable=True),
        *tuning_discoveries(sources.parameters),
        self_tuning_discovery(
            paused=sources.pause.paused,
            scheduler_running=sources.scheduler_running(),
            implementation=_impl(sources.pause),
        ),
        *task_discoveries(
            sources.api_tasks(), owner="src.api", dependencies=API_TASK_DEPENDENCIES
        ),
    ]
    orchestrator = sources.orchestrator()
    if orchestrator is not None:
        engines = orchestrator.signal_engines()
        ensemble = orchestrator.crypto_box.engine_orchestrator
        background = orchestrator.background_tasks()
        rows.extend(signal_model_discoveries(engines))
        rows.extend(
            ensemble_discoveries(
                ensemble,
                producers=CACHE_FIELD_PRODUCERS,
                implementation=_impl(orchestrator.crypto_box),
            )
        )
        rows.extend(provider_task_discoveries(orchestrator.provider_tasks()))
        rows.extend(
            task_discoveries(
                background,
                owner="src.engine",
                dependencies=_orchestrator_task_dependencies(
                    background, engines, ensemble is not None
                ),
            )
        )
        rows.extend(worker_pool_discoveries(orchestrator.worker_pools(), owner="src.engine"))
    intel = sources.intel()
    if intel is not None:
        rows.append(worker_discovery("intel_horizons", intel.worker_pool))
        rows.extend(upgrade_row_discoveries(sources.upgrade_rows, _impl(intel.upgrade_registry)))
    return rows


def production_controllers(sources: ProductionSources) -> dict[ComponentType, Controller]:
    """The three real controllers; every other type stays observe-only."""
    strategy = StrategyKillSwitchController(
        sources.strategies, sources.kill_switch, clock_ms=sources.clock_ms
    )

    def signal_engines() -> Mapping[str, Any]:
        orchestrator = sources.orchestrator()
        return {} if orchestrator is None else orchestrator.signal_engines()

    shadow = ShadowCandidateController(signal_engines)
    self_tuning = SelfTuningController(sources.pause, sources.on_pause_change)
    self_tuning_id = make_component_id(ComponentType.TUNING, SELF_TUNING)

    def route_model(record: ComponentRecord) -> Controller | None:
        # Only candidates declare STOP; the live slot is observe-only.
        return shadow if "/candidate/" in record.component_id else None

    def route_tuning(record: ComponentRecord) -> Controller | None:
        return self_tuning if record.component_id == self_tuning_id else None

    return {
        ComponentType.STRATEGY: strategy,
        ComponentType.MODEL: RoutingController(route_model),
        ComponentType.TUNING: RoutingController(route_tuning),
    }


@dataclass(frozen=True, slots=True)
class SyncReport:
    seen: int
    retired: tuple[str, ...]
    problems: tuple[str, ...]


class RuntimeLoop:
    """
    Continuous discovery and reconciliation. ``step`` is one pass and is what
    tests drive; ``run`` repeats it every ``interval_s`` until ``stop`` is set.
    """

    def __init__(
        self,
        platform: RuntimePlatform,
        discover: Callable[[], Sequence[Discovery]],
        *,
        interval_s: float = 5.0,
        slow_step_ms: float = 50.0,
        slow_refresh: Callable[[], Awaitable[Any]] | None = None,
        slow_refresh_every: int = 60,
    ) -> None:
        if interval_s <= 0 or slow_refresh_every < 1:
            raise ValueError("interval_s must be > 0 and slow_refresh_every >= 1")
        self._platform = platform
        self._discover = discover
        self._interval_s = interval_s
        self._slow_step_ms = slow_step_ms
        # Discovery that needs I/O (the MLflow listing): awaited before the
        # first pass and then every ``slow_refresh_every`` passes.
        self._slow_refresh = slow_refresh
        self._slow_refresh_every = slow_refresh_every
        self._slow_refreshed_at: int | None = None
        # Ids reported by the owners; a known id missing from a pass was
        # dropped by its owner. Seeded with what the platform was built from
        # (the same discovery), so a component dropped before the first pass
        # is noticed too.
        self._known: set[str] = {r.component_id for r in platform.registry.components()}
        self.passes = 0

    def sync_once(self) -> SyncReport:
        registry = self._platform.registry
        try:
            rows = list(self._discover())
        except Exception as exc:  # a broken view must not take the loop down
            log.error("runtime.discovery_failed", error=f"{type(exc).__name__}: {exc}")
            return SyncReport(0, (), (f"discovery failed: {exc}",))
        problems: list[str] = []
        seen: set[str] = set()
        for row in rows:
            cid = row.spec.component_id
            if cid in seen:
                problems.append(f"{cid}: reported twice in one pass")
                continue
            try:
                registry.resync(row.spec, row.state, row.health)
            except RuntimeContractError as exc:
                problems.append(f"{cid}: {exc}")
                continue
            seen.add(cid)
        retired = []
        for cid in sorted(self._known - seen):
            record = registry.find(cid)
            if record is None:
                continue
            if record.state is not LifecycleState.STOPPED:
                registry.observe(
                    cid,
                    LifecycleState.STOPPED,
                    actor="discovery",
                    health=HealthReport(HealthState.UNKNOWN, "no longer reported by its owner"),
                )
            if self._platform.changes.open_change(cid) is None and registry.retire(cid):
                retired.append(cid)
            else:
                seen.add(cid)  # still registered: keep watching it
        self._known = seen
        persister = self._platform.persister
        if persister is not None:
            problems.extend(persister.apply_deferred(registry))
        for problem in problems:
            log.warning("runtime.sync_problem", problem=problem)
        return SyncReport(len(rows), tuple(retired), tuple(problems))

    def step(self) -> list[ReconcileOutcome]:
        """One pass: discover, then reconcile through the change manager."""
        started = time.monotonic()
        self.sync_once()
        outcomes = self._platform.reconciler.reconcile_once(suppress_repeats=True)
        self.passes += 1
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if elapsed_ms > self._slow_step_ms:
            log.warning("runtime.loop_step_slow", elapsed_ms=round(elapsed_ms, 1))
        return outcomes

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            due = (
                self._slow_refreshed_at is None
                or self.passes - self._slow_refreshed_at >= self._slow_refresh_every
            )
            if self._slow_refresh is not None and due:
                self._slow_refreshed_at = self.passes
                try:
                    await self._slow_refresh()
                except Exception as exc:
                    log.error("runtime.slow_refresh_failed", error=f"{type(exc).__name__}: {exc}")
            try:
                self.step()
            except Exception as exc:  # the control plane fails; trading does not
                log.error("runtime.loop_step_failed", error=f"{type(exc).__name__}: {exc}")
            try:
                await self._platform.flush()
            except Exception as exc:
                log.error("runtime.loop_flush_failed", error=f"{type(exc).__name__}: {exc}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)


class UpgradeListing:
    """
    The MLflow-registered artifacts, listed off the event loop (it is network
    I/O) on a single dedicated thread: a listing that hangs ties up only this
    thread -- never the default pool other code awaits -- and no second
    listing starts while one is in flight. A failed or slow listing keeps the
    last good rows, so upgrade components do not look dropped by their owner.
    """

    def __init__(self, sources: ProductionSources, *, timeout_s: float = 10.0) -> None:
        self._sources = sources
        self._timeout_s = timeout_s
        self._executor: ThreadPoolExecutor | None = None
        self._inflight: Future[Any] | None = None

    async def refresh(self) -> list[dict[str, Any]]:
        sources = self._sources
        intel = sources.intel()
        if intel is None:
            sources.upgrade_rows = []
            return sources.upgrade_rows
        if self._inflight is not None and not self._inflight.done():
            log.warning("runtime.upgrade_listing_still_running")
            return sources.upgrade_rows
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="runtime-upgrades"
            )
        self._inflight = self._executor.submit(intel.upgrade_registry.list_registered)
        try:
            rows = await asyncio.wait_for(
                asyncio.wrap_future(self._inflight), timeout=self._timeout_s
            )
        except Exception as exc:  # keep the last listing
            log.warning("runtime.upgrade_listing_failed", error=f"{type(exc).__name__}: {exc}")
            return sources.upgrade_rows
        sources.upgrade_rows = [
            {"name": str(r["name"]), "latest_version": str(r["latest_version"])} for r in rows
        ]
        return sources.upgrade_rows

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
