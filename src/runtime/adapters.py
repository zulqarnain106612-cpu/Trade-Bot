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


def strategy_discoveries(
    registry: _StrategyRegistryLike, kill_switch: _KillSwitchLike | None = None
) -> list[Discovery]:
    """
    Every registered strategy. A strategy its kill switch has disabled is
    QUARANTINED (it stays out until the gauntlet re-enables it); otherwise it
    is ACTIVE -- registration is what puts a strategy on the decision path.
    """
    rows = []
    for strategy in registry.all():
        sid = strategy.strategy_id
        reason: str | None = None
        if (
            kill_switch is not None
            and kill_switch.is_registered(sid)
            and not kill_switch.is_enabled(sid)
        ):
            reason = kill_switch.disabled_reason(sid)
        spec = _spec(
            ComponentType.STRATEGY,
            sid,
            version=str(getattr(strategy, "version", UNVERSIONED)),
            implementation=_impl(strategy),
            owner="src.strategies",
            configuration={"required_capital_fraction": strategy.required_capital_fraction()},
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
    return [
        Discovery(
            _spec(
                ComponentType.UPGRADE,
                str(row["name"]),
                version=str(row["latest_version"]),
                implementation=_impl(registry),
                owner="src.upgrade",
            ),
            LifecycleState.DISCOVERED,
        )
        for row in registry.list_registered()
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


def task_discoveries(tasks: Iterable[asyncio.Task[Any]]) -> list[Discovery]:
    """
    Long-running asyncio tasks. A running task is ACTIVE; one that ended by
    cancellation or return is STOPPED; one that raised is FAILED.
    """
    rows = []
    for task in tasks:
        if not task.done():
            state, health = LifecycleState.ACTIVE, HealthReport(HealthState.HEALTHY, "running")
        elif task.cancelled():
            state, health = LifecycleState.STOPPED, HealthReport(HealthState.UNKNOWN, "cancelled")
        elif task.exception() is not None:
            state = LifecycleState.FAILED
            health = HealthReport(HealthState.UNHEALTHY, repr(task.exception()))
        else:
            state, health = LifecycleState.STOPPED, HealthReport(HealthState.UNKNOWN, "returned")
        rows.append(
            Discovery(
                _spec(
                    ComponentType.TASK,
                    task.get_name(),
                    version=UNVERSIONED,
                    implementation=_impl(task),
                    owner="src.engine",
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
