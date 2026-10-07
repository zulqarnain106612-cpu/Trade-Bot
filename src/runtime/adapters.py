"""
Adapters: read the existing specialised registries into runtime specs.

Each adapter returns ``Discovery`` rows (spec, observed state, health) for
``RuntimeRegistry.register_all``. None of them mutates the registry it reads.

Capabilities are what the underlying subsystem really offers, nothing more:

* strategies, engines, tuning parameters, upgrade artifacts, providers, the
  event bus, worker pools and asyncio tasks are observe-only. A strategy is
  disabled by its kill switch and re-enabled only through the promotion
  gauntlet; a tuning parameter changes only through the tuning runner's
  promotion; engines have no pause. Declaring an action the subsystem cannot
  perform would let bookkeeping claim a state the component is not in.
* a shadow model declares ACTIVATE (promotion, gated by the model registry's
  own ``evaluate_shadow``) and STOP (discard). The live model is observe-only:
  it leaves the live slot only by a shadow being promoted over it.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.runtime.contracts import (
    OBSERVE_ONLY,
    CapabilitySet,
    ComponentSpec,
    ComponentType,
    ComponentVersion,
    DependencyDescriptor,
    HealthReport,
    HealthState,
    LifecycleAction,
    LifecycleState,
    make_component_id,
)

UNVERSIONED = "unversioned"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-/]")

SHADOW_MODEL_CAPABILITIES = CapabilitySet(
    actions=frozenset({LifecycleAction.ACTIVATE, LifecycleAction.STOP}),
)


@dataclass(frozen=True, slots=True)
class Discovery:
    spec: ComponentSpec
    state: LifecycleState
    health: HealthReport = field(default_factory=HealthReport)


def safe_name(raw: str) -> str:
    """Map an external name onto the component-id alphabet."""
    cleaned = _UNSAFE.sub("_", raw.strip())
    return cleaned if cleaned and cleaned[0].isalnum() else f"x{cleaned}"


def _impl(obj: object) -> str:
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


def _spec(
    component_type: ComponentType,
    name: str,
    *,
    version: str,
    implementation: str,
    owner: str,
    configuration: Mapping[str, Any] | None = None,
    capabilities: CapabilitySet = OBSERVE_ONLY,
    dependencies: tuple[DependencyDescriptor, ...] = (),
) -> ComponentSpec:
    return ComponentSpec(
        component_id=make_component_id(component_type, safe_name(name)),
        component_type=component_type,
        version=ComponentVersion.create(version, implementation, configuration),
        owner=owner,
        capabilities=capabilities,
        dependencies=dependencies,
    )


# -- strategies ------------------------------------------------------------


class _StrategyLike(Protocol):
    strategy_id: str

    def required_capital_fraction(self) -> float: ...


class _StrategyRegistryLike(Protocol):
    def all(self) -> tuple[Any, ...]: ...


class _KillSwitchLike(Protocol):
    def is_registered(self, strategy_id: str) -> bool: ...

    def is_enabled(self, strategy_id: str) -> bool: ...

    def disabled_reason(self, strategy_id: str) -> str: ...


QUARANTINE_ONLY = CapabilitySet(actions=frozenset({LifecycleAction.QUARANTINE}))


def strategy_discoveries(
    registry: _StrategyRegistryLike,
    kill_switch: _KillSwitchLike | None = None,
    *,
    quarantinable: bool = False,
) -> list[Discovery]:
    """
    Every registered strategy. A strategy its kill switch has disabled is
    QUARANTINED (it stays out until the gauntlet re-enables it); otherwise it
    is ACTIVE -- registration is what puts a strategy on the decision path.

    ``quarantinable`` (production, with a kill switch whose ``disable`` the
    runtime controller calls): a strategy that has a kill switch declares
    QUARANTINE. One without a switch has nothing to disable it with and stays
    observe-only.
    """
    rows = []
    for strategy in registry.all():
        sid = strategy.strategy_id
        reason: str | None = None
        switched = kill_switch is not None and kill_switch.is_registered(sid)
        if switched and kill_switch is not None and not kill_switch.is_enabled(sid):
            reason = kill_switch.disabled_reason(sid)
        spec = _spec(
            ComponentType.STRATEGY,
            sid,
            version=str(getattr(strategy, "version", UNVERSIONED)),
            implementation=_impl(strategy),
            owner="src.strategies",
            configuration={"required_capital_fraction": strategy.required_capital_fraction()},
            capabilities=QUARANTINE_ONLY if quarantinable and switched else OBSERVE_ONLY,
        )
        if reason is None:
            state, health = LifecycleState.ACTIVE, HealthReport(HealthState.HEALTHY, "enabled")
        else:
            state, health = LifecycleState.QUARANTINED, HealthReport(HealthState.UNHEALTHY, reason)
        rows.append(Discovery(spec, state, health))
    return rows


# -- models ----------------------------------------------------------------


class _ModelRegistryLike(Protocol):
    @property
    def live_model_id(self) -> str | None: ...

    def shadow_ids(self) -> list[str]: ...


def model_discoveries(registry: _ModelRegistryLike) -> list[Discovery]:
    """The live model (ACTIVE, observe-only) and each shadow (STANDBY)."""
    impl = _impl(registry)
    rows = []
    live = registry.live_model_id
    if live is not None:
        rows.append(
            Discovery(
                _spec(
                    ComponentType.MODEL,
                    live,
                    version=live,
                    implementation=impl,
                    owner="src.models",
                    configuration={"model_id": live},
                ),
                LifecycleState.ACTIVE,
                HealthReport(HealthState.HEALTHY, "live"),
            )
        )
    for shadow in registry.shadow_ids():
        rows.append(
            Discovery(
                _spec(
                    ComponentType.MODEL,
                    shadow,
                    version=shadow,
                    implementation=impl,
                    owner="src.models",
                    configuration={"model_id": shadow},
                    capabilities=SHADOW_MODEL_CAPABILITIES,
                ),
                LifecycleState.STANDBY,
                HealthReport(HealthState.HEALTHY, "shadow evaluation"),
            )
        )
    return rows


# -- engines ---------------------------------------------------------------


class _EngineOrchestratorLike(Protocol):
    def engine_roster(self) -> tuple[tuple[str, str], ...]: ...


def engine_discoveries(orchestrator: _EngineOrchestratorLike) -> list[Discovery]:
    """E-01..E-18 in the orchestrator's positional order (SIG-004)."""
    return [
        Discovery(
            _spec(
                ComponentType.ENGINE,
                engine_id,
                version=UNVERSIONED,
                implementation=implementation,
                owner="src.engines",
            ),
            LifecycleState.ACTIVE,
        )
        for engine_id, implementation in orchestrator.engine_roster()
    ]


# -- tuning ----------------------------------------------------------------


class _ParameterLike(Protocol):
    name: str
    floor: float
    ceiling: float
    current: float
    eval_strategy: str


class _ParameterRegistryLike(Protocol):
    def list_all(self) -> list[Any]: ...


def tuning_discoveries(registry: _ParameterRegistryLike) -> list[Discovery]:
    """Each tunable parameter; its version is the current champion value."""
    rows = []
    for param in registry.list_all():
        rows.append(
            Discovery(
                _spec(
                    ComponentType.TUNING,
                    param.name,
                    version=repr(float(param.current)),
                    implementation=_impl(param),
                    owner="src.tuning",
                    configuration={
                        "floor": param.floor,
                        "ceiling": param.ceiling,
                        "current": param.current,
                        "eval_strategy": param.eval_strategy,
                    },
                ),
                LifecycleState.ACTIVE,
            )
        )
    return rows


# -- upgrade ---------------------------------------------------------------


class _UpgradeRegistryLike(Protocol):
    def list_registered(self, model_name_prefix: str = "") -> list[dict[str, Any]]: ...


def upgrade_discoveries(registry: _UpgradeRegistryLike) -> list[Discovery]:
    """Registered model artifacts: known, not loaded -- DISCOVERED."""
    return upgrade_row_discoveries(registry.list_registered(), _impl(registry))


def upgrade_row_discoveries(
    rows: Iterable[Mapping[str, Any]], implementation: str
) -> list[Discovery]:
    """
    The same, from rows already fetched: listing the MLflow registry is
    network I/O, so production fetches it off the event loop and hands the
    rows here.
    """
    return [
        Discovery(
            _spec(
                ComponentType.UPGRADE,
                str(row["name"]),
                version=str(row["latest_version"]),
                implementation=implementation,
                owner="src.upgrade",
            ),
            LifecycleState.DISCOVERED,
        )
        for row in rows
    ]


# -- workers, tasks, providers, event bus ----------------------------------


class _WorkerPoolLike(Protocol):
    @property
    def worker_count(self) -> int: ...


def worker_discovery(name: str, pool: _WorkerPoolLike) -> Discovery:
    count = pool.worker_count
    return Discovery(
        _spec(
            ComponentType.WORKER,
            name,
            version=UNVERSIONED,
            implementation=_impl(pool),
            owner="src.workers",
        ),
        LifecycleState.ACTIVE if count > 0 else LifecycleState.STOPPED,
        HealthReport(
            HealthState.HEALTHY if count > 0 else HealthState.UNHEALTHY, f"{count} workers"
        ),
    )


def task_state(task: asyncio.Task[Any]) -> tuple[LifecycleState, HealthReport]:
    """A running task is ACTIVE; cancelled or returned, STOPPED; raised, FAILED."""
    if not task.done():
        return LifecycleState.ACTIVE, HealthReport(HealthState.HEALTHY, "running")
    if task.cancelled():
        return LifecycleState.STOPPED, HealthReport(HealthState.UNKNOWN, "cancelled")
    if task.exception() is not None:
        return LifecycleState.FAILED, HealthReport(HealthState.UNHEALTHY, repr(task.exception()))
    return LifecycleState.STOPPED, HealthReport(HealthState.UNKNOWN, "returned")


def task_discoveries(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    owner: str = "src.engine",
    dependencies: Mapping[str, tuple[DependencyDescriptor, ...]] | None = None,
) -> list[Discovery]:
    """
    Long-running asyncio tasks. A running task is ACTIVE; one that ended by
    cancellation or return is STOPPED; one that raised is FAILED.
    ``dependencies`` by task name.
    """
    deps = dependencies or {}
    rows = []
    for task in tasks:
        state, health = task_state(task)
        rows.append(
            Discovery(
                _spec(
                    ComponentType.TASK,
                    task.get_name(),
                    version=UNVERSIONED,
                    implementation=_impl(task),
                    owner=owner,
                    dependencies=deps.get(task.get_name(), ()),
                ),
                state,
                health,
            )
        )
    return rows


class _ProviderLike(Protocol):
    @property
    def exchange_id(self) -> str: ...


def provider_discoveries(providers: Iterable[_ProviderLike]) -> list[Discovery]:
    return [
        Discovery(
            _spec(
                ComponentType.PROVIDER,
                provider.exchange_id,
                version=UNVERSIONED,
                implementation=_impl(provider),
                owner="src.intelligence",
            ),
            LifecycleState.ACTIVE,
        )
        for provider in providers
    ]


class _EventBusLike(Protocol):
    @property
    def subscriber_count(self) -> int: ...

    @property
    def total_dropped(self) -> int: ...


def eventbus_discovery(bus: _EventBusLike, name: str = "main") -> Discovery:
    dropped = bus.total_dropped
    return Discovery(
        _spec(
            ComponentType.EVENTBUS,
            name,
            version=UNVERSIONED,
            implementation=_impl(bus),
            owner="src.eventbus",
        ),
        LifecycleState.ACTIVE,
        HealthReport(
            HealthState.DEGRADED if dropped else HealthState.HEALTHY,
            f"{bus.subscriber_count} subscribers, {dropped} dropped",
        ),
    )


# -- production views ------------------------------------------------------
#
# The adapters below read the running process's own objects through the
# read-only views those objects expose (src/engine/orchestrator.py,
# src/engine/signal_engine.py, src/engines/orchestrator.py); production.py
# assembles them.

SHADOW_DISCARD_CAPABILITIES = CapabilitySet(actions=frozenset({LifecycleAction.STOP}))
SELF_TUNING_CAPABILITIES = CapabilitySet(
    actions=frozenset({LifecycleAction.PAUSE, LifecycleAction.RESUME})
)

# An engine that failed this many cycles running is UNHEALTHY; fewer, DEGRADED.
ENGINE_UNHEALTHY_AFTER = 3


class _SignalEngineLike(Protocol):
    @property
    def live_model_id(self) -> str | None: ...

    @property
    def shadow_model_id(self) -> str | None: ...

    def shadow_evaluations(self) -> int: ...


def live_model_id(timeframe: str) -> str:
    """The component id of a timeframe's live-model slot."""
    return make_component_id(ComponentType.MODEL, safe_name(f"{timeframe}/live"))


def signal_model_discoveries(engines: Mapping[str, _SignalEngineLike]) -> list[Discovery]:
    """
    Per timeframe: the live-model slot (ACTIVE, observe-only; its version is
    the model trading now) and, while one is under evaluation, the shadow
    candidate (STANDBY, may be discarded). A candidate is its own component
    -- ``model:<tf>/candidate/<model id>`` -- so discarding one says nothing
    about the next candidate a retrain produces.
    """
    rows = []
    for timeframe, engine in sorted(engines.items()):
        live = engine.live_model_id
        rows.append(
            Discovery(
                _spec(
                    ComponentType.MODEL,
                    f"{timeframe}/live",
                    version=live or "none",
                    implementation=_impl(engine),
                    owner="src.engine",
                    configuration={"timeframe": timeframe, "model_id": live},
                ),
                LifecycleState.ACTIVE if live else LifecycleState.STOPPED,
                HealthReport(
                    HealthState.HEALTHY if live else HealthState.UNHEALTHY,
                    f"live: {live}" if live else "no live model",
                ),
            )
        )
        shadow = engine.shadow_model_id
        if shadow is None:
            continue
        rows.append(
            Discovery(
                _spec(
                    ComponentType.MODEL,
                    f"{timeframe}/candidate/{shadow}",
                    version=shadow,
                    implementation=_impl(engine),
                    owner="src.engine",
                    configuration={"timeframe": timeframe, "model_id": shadow},
                    capabilities=SHADOW_DISCARD_CAPABILITIES,
                    # Scored against the live model's predictions on the same bars.
                    dependencies=(DependencyDescriptor(live_model_id(timeframe)),),
                ),
                LifecycleState.STANDBY,
                HealthReport(
                    HealthState.HEALTHY,
                    f"shadow evaluation: {engine.shadow_evaluations()} resolved predictions",
                ),
            )
        )
    return rows


def run_health(stats: Mapping[str, Any] | None) -> HealthReport:
    """Health from an engine's run record (EngineRunStats.snapshot)."""
    if not stats or not stats.get("runs"):
        return HealthReport(HealthState.UNKNOWN, "not run yet")
    runs, failures = int(stats["runs"]), int(stats["failures"])
    if stats.get("last_ok"):
        latency = stats.get("last_latency_ms")
        took = "" if latency is None else f" in {float(latency):.1f} ms"
        return HealthReport(HealthState.HEALTHY, f"ok{took}; {failures}/{runs} cycles failed")
    consecutive = int(stats.get("consecutive_failures", 0))
    state = HealthState.UNHEALTHY if consecutive >= ENGINE_UNHEALTHY_AFTER else HealthState.DEGRADED
    return HealthReport(
        state, f"{stats.get('last_error')}; {consecutive} consecutive, {failures}/{runs} failed"
    )


class _EnsembleLike(Protocol):
    def engine_roster(self) -> tuple[tuple[str, str], ...]: ...

    def stage_roster(self) -> tuple[tuple[str, str], ...]: ...

    def engine_health(self) -> dict[str, dict[str, Any]]: ...

    def cache_inputs(self) -> dict[str, tuple[str, ...]]: ...

    def stage_inputs(self) -> dict[str, tuple[tuple[str, bool], ...]]: ...


ENSEMBLE = "ensemble"


def ensemble_discoveries(
    ensemble: _EnsembleLike | None,
    *,
    producers: Mapping[str, str],
    implementation: str,
) -> list[Discovery]:
    """
    The Crypto-Box ensemble as the trading tick calls it: ``engine:ensemble``
    (ACTIVE when CRYPTO_BOX is on, STOPPED otherwise), and when on, E-01..E-18
    in positional order (SIG-004) plus consensus, risk quantifier and signal
    gate, each with health from its own run record.

    Dependencies are what the code reads: an engine on the component that
    fills its provider-cache field (``cache_inputs`` field -> ``producers``
    component; optional, every engine degrades without it), each stage on
    the engines and stages it consumes (``stage_inputs``), and the ensemble
    on the signal gate whose verdict it returns. Both tables come from the
    ensemble itself (``cache_inputs``, ``stage_inputs``).

    Observe-only throughout. Taking an engine out of consensus is not a risk
    reduction -- E-16 is a manipulation veto, E-11/E-17 feed the tail-risk
    score -- so no action here could be classified honestly as LIVE_SAFE.
    """
    if ensemble is None:
        return [
            Discovery(
                _spec(
                    ComponentType.ENGINE,
                    ENSEMBLE,
                    version=UNVERSIONED,
                    implementation=implementation,
                    owner="src.engine",
                ),
                LifecycleState.STOPPED,
                HealthReport(HealthState.UNKNOWN, "CRYPTO_BOX is not enabled"),
            )
        ]
    health = ensemble.engine_health()
    cache_inputs = ensemble.cache_inputs()
    stage_inputs = ensemble.stage_inputs()
    rows = []
    for position, (engine_id, engine_impl) in enumerate(ensemble.engine_roster(), start=1):
        deps = tuple(
            DependencyDescriptor(producers[field], required=False)
            for field in cache_inputs.get(engine_id, ())
            if field in producers
        )
        rows.append(
            Discovery(
                _spec(
                    ComponentType.ENGINE,
                    engine_id,
                    version=UNVERSIONED,
                    implementation=engine_impl,
                    owner="src.engines",
                    configuration={"position": position},
                    dependencies=deps,
                ),
                LifecycleState.ACTIVE,
                run_health(health.get(engine_id)),
            )
        )
    for stage_id, stage_impl in ensemble.stage_roster():
        rows.append(
            Discovery(
                _spec(
                    ComponentType.ENGINE,
                    stage_id,
                    version=UNVERSIONED,
                    implementation=stage_impl,
                    owner="src.engines",
                    dependencies=tuple(
                        DependencyDescriptor(
                            make_component_id(ComponentType.ENGINE, safe_name(dep)),
                            required=required,
                        )
                        for dep, required in stage_inputs.get(stage_id, ())
                    ),
                ),
                LifecycleState.ACTIVE,
                run_health(health.get(stage_id)),
            )
        )
    rows.append(
        Discovery(
            _spec(
                ComponentType.ENGINE,
                ENSEMBLE,
                version=UNVERSIONED,
                implementation=implementation,
                owner="src.engine",
                dependencies=(
                    DependencyDescriptor(make_component_id(ComponentType.ENGINE, "signal_gate")),
                ),
            ),
            LifecycleState.ACTIVE,
            run_health(health.get("signal_gate")),
        )
    )
    return rows


def provider_task_discoveries(
    provider_tasks: Mapping[str, Iterable[asyncio.Task[Any]]],
) -> list[Discovery]:
    """
    A data provider per provider-cache field, from the loops that fill it:
    FAILED if any loop raised, ACTIVE while any runs (DEGRADED if not all),
    STOPPED when none does.
    """
    rows = []
    for name, group in sorted(provider_tasks.items()):
        tasks = list(group)
        states = [task_state(t) for t in tasks]
        running = sum(1 for st, _ in states if st is LifecycleState.ACTIVE)
        failed = [h for st, h in states if st is LifecycleState.FAILED]
        if failed:
            state, health = LifecycleState.FAILED, failed[0]
        elif running:
            state = LifecycleState.ACTIVE
            health = HealthReport(
                HealthState.HEALTHY if running == len(tasks) else HealthState.DEGRADED,
                f"{running}/{len(tasks)} loops running",
            )
        else:
            state, health = LifecycleState.STOPPED, HealthReport(HealthState.UNKNOWN, "stopped")
        rows.append(
            Discovery(
                _spec(
                    ComponentType.PROVIDER,
                    name,
                    version=UNVERSIONED,
                    implementation=_impl(tasks[0]) if tasks else "asyncio.Task",
                    owner="src.data",
                    configuration={"tasks": sorted(t.get_name() for t in tasks)},
                ),
                state,
                health,
            )
        )
    return rows


def worker_pool_discoveries(
    pools: Mapping[str, Mapping[str, Any]], *, owner: str
) -> list[Discovery]:
    """Thread pools by name: ``workers`` (configured), ``available``, ``detail``."""
    rows = []
    for name, status in sorted(pools.items()):
        workers = int(status["workers"])
        available = bool(status["available"])
        rows.append(
            Discovery(
                _spec(
                    ComponentType.WORKER,
                    name,
                    version=UNVERSIONED,
                    implementation="concurrent.futures.ThreadPoolExecutor",
                    owner=owner,
                    configuration={"workers": workers},
                ),
                LifecycleState.ACTIVE if workers > 0 else LifecycleState.STOPPED,
                HealthReport(
                    HealthState.HEALTHY if available else HealthState.UNHEALTHY,
                    f"{workers} workers; {status.get('detail', '')}".rstrip("; "),
                ),
            )
        )
    return rows


SELF_TUNING = "self_tuning"


def self_tuning_discovery(
    *, paused: bool, scheduler_running: bool, implementation: str
) -> Discovery:
    """
    The self-tuning switch: PAUSED while paused, ACTIVE otherwise. The switch
    is the component, so its state is meaningful whether or not the scheduler
    runs (SELF_TUNING_ENABLED); the health says which.
    """
    detail = "scheduler running" if scheduler_running else "scheduler not started"
    return Discovery(
        _spec(
            ComponentType.TUNING,
            SELF_TUNING,
            version=UNVERSIONED,
            implementation=implementation,
            owner="src.tuning",
            configuration={"scheduler_running": scheduler_running},
            capabilities=SELF_TUNING_CAPABILITIES,
        ),
        LifecycleState.PAUSED if paused else LifecycleState.ACTIVE,
        HealthReport(
            HealthState.HEALTHY if scheduler_running else HealthState.UNKNOWN,
            f"{'paused' if paused else 'running'}; {detail}",
        ),
    )
