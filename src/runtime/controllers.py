"""
Concrete controllers: the runtime platform's only hands on the production
subsystems. Each performs one operation its subsystem already owns, through
that subsystem's public method, and declares nothing it cannot do:

* ``StrategyKillSwitchController`` -- QUARANTINE disables the strategy through
  its kill switch, the same switch drift pulls (decision log, ``killswitch``
  event). Only the promotion gauntlet re-enables a strategy
  (``POST /strategies/{id}/re-enable``), so nothing here can put one back on
  the decision path.
* ``ShadowCandidateController`` -- STOP discards a timeframe's shadow model
  candidate. A shadow never influences trading; discarding one only means it
  can no longer promote itself. Promotion is not offered: the signal engine
  promotes a shadow itself, on its own evaluation, under its own model lock
  -- a second path into the live slot would be a second gate.
* ``SelfTuningController`` -- PAUSE/RESUME of self-tuning, the switch
  ``/self-tuning/pause`` flips. Paused, no tuning attempt starts; resuming is
  approval-gated by its change class.
* ``RoutingController`` -- one supervisor runs a whole component type; this
  dispatches by component id inside it, observe-only for everything else.

Every controller here is synchronous and runs on the event-loop thread, inside
a change-manager call made by an API handler or the runtime loop: no coroutine
interleaves with it, which is what makes the lock checks in the subsystems'
synchronous entry points sound.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from src.runtime.adapters import safe_name
from src.runtime.contracts import (
    ComponentRecord,
    ComponentType,
    ComponentVersion,
    HealthReport,
    HealthState,
    LifecycleAction,
    UnsupportedActionError,
    make_component_id,
)
from src.runtime.supervisor import ActionRefusedError, Controller, ObserveOnlyController

A = LifecycleAction

QUARANTINE_REASON = "quarantined through the runtime platform"
DISCARD_REASON = "discarded through the runtime platform"


# -- strategies ------------------------------------------------------------


class _StrategyRegistryLike(Protocol):
    def all(self) -> tuple[Any, ...]: ...


class _DisablingKillSwitch(Protocol):
    def is_registered(self, strategy_id: str) -> bool: ...

    def is_enabled(self, strategy_id: str) -> bool: ...

    def disabled_reason(self, strategy_id: str) -> str: ...

    def disable(self, strategy_id: str, reason: str, *, now_ms: int = 0) -> bool: ...


def strategy_id_of(registry: _StrategyRegistryLike, component_id: str) -> str | None:
    """The registered strategy a ``strategy:`` component id names, if any."""
    for strategy in registry.all():
        sid = str(strategy.strategy_id)
        if make_component_id(ComponentType.STRATEGY, safe_name(sid)) == component_id:
            return sid
    return None


class StrategyKillSwitchController:
    def __init__(
        self,
        strategies: _StrategyRegistryLike,
        kill_switch: _DisablingKillSwitch,
        *,
        clock_ms: Callable[[], int],
    ) -> None:
        self._strategies = strategies
        self._kill_switch = kill_switch
        self._clock_ms = clock_ms

    def _strategy_id(self, record: ComponentRecord) -> str:
        sid = strategy_id_of(self._strategies, record.component_id)
        if sid is None:
            raise ActionRefusedError(f"{record.component_id} is no longer a registered strategy")
        if not self._kill_switch.is_registered(sid):
            raise ActionRefusedError(f"{sid} has no kill switch to disable it with")
        return sid

    def perform(
        self,
        record: ComponentRecord,
        action: LifecycleAction,
        target_version: ComponentVersion | None,
    ) -> None:
        if action is not A.QUARANTINE:
            raise UnsupportedActionError(record.component_id, action, "kill-switch controller")
        sid = self._strategy_id(record)
        # False only when it was already disabled: the strategy is out either way.
        self._kill_switch.disable(sid, QUARANTINE_REASON, now_ms=self._clock_ms())

    def probe(self, record: ComponentRecord) -> HealthReport:
        sid = strategy_id_of(self._strategies, record.component_id)
        if sid is None:
            return HealthReport(HealthState.UNKNOWN, "no longer registered")
        if not self._kill_switch.is_registered(sid) or self._kill_switch.is_enabled(sid):
            return HealthReport(HealthState.HEALTHY, "enabled")
        return HealthReport(HealthState.UNHEALTHY, self._kill_switch.disabled_reason(sid))


# -- shadow model candidates -----------------------------------------------


class _ShadowHolder(Protocol):
    @property
    def live_model_id(self) -> str | None: ...

    @property
    def shadow_model_id(self) -> str | None: ...

    def discard_shadow_now(self, reason: str) -> bool: ...


def model_slot(record: ComponentRecord) -> tuple[str, str]:
    """(timeframe, model id) from a model component's configuration."""
    config = record.version.configuration
    return str(config.get("timeframe", "")), str(config.get("model_id", record.version.version))


class ShadowCandidateController:
    def __init__(self, engines: Callable[[], Mapping[str, _ShadowHolder]]) -> None:
        self._engines = engines

    def _engine(self, record: ComponentRecord) -> tuple[_ShadowHolder, str]:
        timeframe, model_id = model_slot(record)
        engine = self._engines().get(timeframe)
        if engine is None:
            raise ActionRefusedError(f"no signal engine runs timeframe {timeframe!r}")
        return engine, model_id

    def perform(
        self,
        record: ComponentRecord,
        action: LifecycleAction,
        target_version: ComponentVersion | None,
    ) -> None:
        if action is not A.STOP:
            raise UnsupportedActionError(record.component_id, action, "shadow controller")
        engine, model_id = self._engine(record)
        if engine.shadow_model_id != model_id:
            # Promoted or superseded since it was observed: there is nothing
            # left to discard, and discarding the newer candidate instead
            # would act on something nobody asked about.
            raise ActionRefusedError(f"{model_id} is no longer the shadow candidate")
        try:
            engine.discard_shadow_now(DISCARD_REASON)
        except RuntimeError as exc:  # a tick holds the model lock: try again, nothing changed
            raise ActionRefusedError(str(exc)) from exc

    def probe(self, record: ComponentRecord) -> HealthReport:
        try:
            engine, model_id = self._engine(record)
        except ActionRefusedError as exc:
            return HealthReport(HealthState.UNKNOWN, str(exc))
        if engine.shadow_model_id == model_id:
            return HealthReport(HealthState.HEALTHY, "shadow evaluation")
        if engine.live_model_id == model_id:
            return HealthReport(HealthState.HEALTHY, "promoted to live")
        return HealthReport(HealthState.UNKNOWN, "no longer held by its signal engine")


# -- self-tuning -----------------------------------------------------------


class _PauseSwitch(Protocol):
    @property
    def paused(self) -> bool: ...

    def set_paused_now(self, value: bool) -> None: ...


class SelfTuningController:
    def __init__(self, pause: _PauseSwitch, on_change: Callable[[bool], None]) -> None:
        self._pause = pause
        # Records the switch in the self-tuning audit log, as the
        # /self-tuning/pause and /resume endpoints do.
        self._on_change = on_change

    def perform(
        self,
        record: ComponentRecord,
        action: LifecycleAction,
        target_version: ComponentVersion | None,
    ) -> None:
        if action not in (A.PAUSE, A.RESUME):
            raise UnsupportedActionError(record.component_id, action, "self-tuning controller")
        paused = action is A.PAUSE
        try:
            self._pause.set_paused_now(paused)
        except RuntimeError as exc:  # the switch is mid-change: try again, nothing changed
            raise ActionRefusedError(str(exc)) from exc
        self._on_change(paused)

    def probe(self, record: ComponentRecord) -> HealthReport:
        if self._pause.paused:
            return HealthReport(HealthState.HEALTHY, "paused: no tuning attempt starts")
        return HealthReport(HealthState.HEALTHY, "running")


# -- dispatch --------------------------------------------------------------


class RoutingController:
    """One controller per component id inside a type; observe-only otherwise."""

    def __init__(
        self,
        route: Callable[[ComponentRecord], Controller | None],
        fallback: Controller | None = None,
    ) -> None:
        self._route = route
        self._fallback = fallback or ObserveOnlyController()

    def _for(self, record: ComponentRecord) -> Controller:
        return self._route(record) or self._fallback

    def perform(
        self,
        record: ComponentRecord,
        action: LifecycleAction,
        target_version: ComponentVersion | None,
    ) -> None:
        self._for(record).perform(record, action, target_version)

    def probe(self, record: ComponentRecord) -> HealthReport:
        return self._for(record).probe(record)
