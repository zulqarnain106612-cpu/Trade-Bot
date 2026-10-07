"""
Supervisors: execute lifecycle actions through per-type controllers.

A supervisor owns one component type. For every action it

1. previews the transition in the registry (the lifecycle table, the
   component's capabilities and the version rules) -- an illegal request
   never reaches a controller;
2. takes the component's transition lock without waiting -- two concurrent
   actions on one component are refused, not interleaved;
3. asks the controller to perform the action;
4. records the result: ``apply`` on success, ``mark_failed`` when the
   controller raised, followed by the restart/quarantine policy. A controller
   that *declines* (``ActionRefusedError``: a gate the subsystem owns was not
   passed) leaves the component exactly as it was.

Controllers are the only code that touches a subsystem. They call methods the
subsystem's own package provides; none loads or patches code.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import ClassVar, Protocol

import structlog

from src.runtime.contracts import (
    ComponentRecord,
    ComponentType,
    ComponentVersion,
    HealthReport,
    HealthState,
    LifecycleAction,
    LifecycleState,
    TransitionRecord,
    UnsupportedActionError,
)
from src.runtime.registry import RuntimeRegistry

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class SupervisorError(RuntimeError):
    """Base for supervisor refusals and failures."""


class WrongSupervisorError(SupervisorError):
    """The component belongs to another supervisor's type."""


class TransitionInProgressError(SupervisorError):
    """Another action on this component has not finished."""


class ActionFailedError(SupervisorError):
    """The controller raised; the component is now FAILED (or QUARANTINED)."""

    def __init__(self, transition: TransitionRecord) -> None:
        self.transition = transition
        super().__init__(f"{transition.component_id}: {transition.error}")


class ActionRefusedError(SupervisorError):
    """
    The subsystem declined the action and changed nothing (a gate it owns was
    not passed). The component keeps its state: a refusal is not a failure.
    """


class RestartBudgetExhaustedError(SupervisorError):
    """The component has used every restart its policy allows."""


class Controller(Protocol):
    def perform(
        self,
        record: ComponentRecord,
        action: LifecycleAction,
        target_version: ComponentVersion | None,
    ) -> None:
        """Drive the subsystem through ``action``; raise if it did not happen."""

    def probe(self, record: ComponentRecord) -> HealthReport:
        """Read the component's health from the subsystem."""


class ObserveOnlyController:
    """For components that declare no actions: health is what was observed."""

    def perform(
        self,
        record: ComponentRecord,
        action: LifecycleAction,
        target_version: ComponentVersion | None,
    ) -> None:
        raise UnsupportedActionError(record.component_id, action, "observe-only component")

    def probe(self, record: ComponentRecord) -> HealthReport:
        return record.health


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    """
    ``max_restarts`` restarts per component; past that a failure quarantines
    the component when it declares QUARANTINE and otherwise leaves it FAILED.
    """

    max_restarts: int = 3

    def __post_init__(self) -> None:
        if self.max_restarts < 0:
            raise ValueError("max_restarts must be >= 0")


class Supervisor:
    component_type: ClassVar[ComponentType | None] = None

    def __init__(
        self,
        registry: RuntimeRegistry,
        controller: Controller,
        *,
        policy: RestartPolicy | None = None,
        component_type: ComponentType | None = None,
    ) -> None:
        resolved = component_type or type(self).component_type
        if resolved is None:
            raise ValueError("a supervisor needs a component type")
        self.supervised_type: ComponentType = resolved
        self._registry = registry
        self._controller = controller
        self._policy = policy or RestartPolicy()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, component_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(component_id, threading.Lock())

    def _record(self, component_id: str) -> ComponentRecord:
        record = self._registry.get(component_id)
        if record.spec.component_type is not self.supervised_type:
            raise WrongSupervisorError(
                f"{component_id} is a {record.spec.component_type.value}, "
                f"this supervisor runs {self.supervised_type.value}"
            )
        return record

    def execute(
        self,
        component_id: str,
        action: LifecycleAction,
        *,
        actor: str,
        target_version: ComponentVersion | None = None,
        change_id: str | None = None,
    ) -> TransitionRecord:
        self._record(component_id)
        lock = self._lock_for(component_id)
        if not lock.acquire(blocking=False):
            raise TransitionInProgressError(f"{component_id}: another action is in progress")
        try:
            return self._execute_locked(
                component_id, action, actor=actor, target_version=target_version, change_id=change_id
            )
        finally:
            lock.release()

    def _execute_locked(
        self,
        component_id: str,
        action: LifecycleAction,
        *,
        actor: str,
        target_version: ComponentVersion | None,
        change_id: str | None,
    ) -> TransitionRecord:
        # Re-read under the lock: the state may have moved since the caller looked.
        record = self._registry.get(component_id)
        _, resolved_version = self._registry.preview(component_id, action, target_version)
        swap = action in (LifecycleAction.REPLACE, LifecycleAction.ROLLBACK)
        try:
            self._controller.perform(record, action, resolved_version if swap else None)
        except ActionRefusedError as exc:
            log.info(
                "runtime.action_refused", component_id=component_id, action=action.value, reason=str(exc)
            )
            raise
        except Exception as exc:  # the controller's failure is the component's failure
            failed = self._registry.mark_failed(
                component_id, action, f"{type(exc).__name__}: {exc}", actor=actor, change_id=change_id
            )
            log.warning(
                "runtime.action_failed",
                component_id=component_id,
                action=action.value,
                error=failed.error,
            )
            self._after_failure(component_id, actor=actor, change_id=change_id)
            raise ActionFailedError(failed) from exc
        transition = self._registry.apply(
            component_id,
            action,
            actor=actor,
            target_version=resolved_version if swap else None,
            change_id=change_id,
        )
        log.info(
            "runtime.action_applied",
            component_id=component_id,
            action=action.value,
            to_state=transition.to_state.value,
            to_version=transition.to_version,
        )
        self._after_success(record, action, actor=actor)
        return transition

    def _after_success(self, record: ComponentRecord, action: LifecycleAction, *, actor: str) -> None:
        """Hook for side effects on other components (see ModelSupervisor)."""

    def _after_failure(self, component_id: str, *, actor: str, change_id: str | None) -> None:
        record = self._registry.get(component_id)
        if record.failure_count <= self._policy.max_restarts:
            return
        if LifecycleAction.QUARANTINE not in record.spec.capabilities.actions:
            self._registry.record_health(
                component_id,
                HealthReport(
                    HealthState.UNHEALTHY,
                    f"failed {record.failure_count} times; quarantine not supported",
                    record.updated_at,
                ),
            )
            return
        try:
            self._controller.perform(record, LifecycleAction.QUARANTINE, None)
        except Exception as exc:  # quarantine itself failed: stay FAILED, say so
            self._registry.record_health(
                component_id,
                HealthReport(
                    HealthState.UNHEALTHY,
                    f"quarantine failed: {type(exc).__name__}: {exc}",
                    record.updated_at,
                ),
            )
            log.error("runtime.quarantine_failed", component_id=component_id, error=str(exc))
            return
        self._registry.apply(
            component_id, LifecycleAction.QUARANTINE, actor=actor, change_id=change_id
        )
        log.error("runtime.quarantined", component_id=component_id)

    def restart(self, component_id: str, *, actor: str, change_id: str | None = None) -> TransitionRecord:
        """STOP then START a FAILED component, within the restart budget."""
        record = self._record(component_id)
        if record.restart_count >= self._policy.max_restarts:
            raise RestartBudgetExhaustedError(
                f"{component_id}: {record.restart_count} restarts used "
                f"of {self._policy.max_restarts}"
            )
        if record.state is not LifecycleState.FAILED:
            raise SupervisorError(f"{component_id}: restart is for FAILED components")
        self.execute(component_id, LifecycleAction.STOP, actor=actor, change_id=change_id)
        self._registry.note_restart(component_id)
        return self.execute(component_id, LifecycleAction.START, actor=actor, change_id=change_id)

    def probe(self, component_id: str) -> HealthReport:
        record = self._record(component_id)
        try:
            report = self._controller.probe(record)
        except Exception as exc:  # an unreadable component is unhealthy, not healthy
            report = HealthReport(
                HealthState.UNHEALTHY, f"probe failed: {type(exc).__name__}: {exc}", record.updated_at
            )
            log.warning("runtime.probe_failed", component_id=component_id, error=report.detail)
        self._registry.record_health(component_id, report)
        return report

    def probe_all(self) -> dict[str, HealthReport]:
        return {
            r.component_id: self.probe(r.component_id)
            for r in self._registry.components(self.supervised_type)
        }


class EngineSupervisor(Supervisor):
    component_type = ComponentType.ENGINE


class StrategySupervisor(Supervisor):
    component_type = ComponentType.STRATEGY


class TaskSupervisor(Supervisor):
    component_type = ComponentType.TASK


class WorkerSupervisor(Supervisor):
    component_type = ComponentType.WORKER


class ProviderSupervisor(Supervisor):
    component_type = ComponentType.PROVIDER


class TuningSupervisor(Supervisor):
    component_type = ComponentType.TUNING


class EventBusSupervisor(Supervisor):
    component_type = ComponentType.EVENTBUS


class UpgradeSupervisor(Supervisor):
    component_type = ComponentType.UPGRADE


class PromotionRefusedError(ActionRefusedError):
    """The model registry's own evaluation does not allow this promotion."""


class _ModelRegistryLike(Protocol):
    @property
    def live_model_id(self) -> str | None: ...

    def shadow_ids(self) -> list[str]: ...

    def evaluate_shadow(self, model_id: str) -> tuple[bool, str]: ...

    def promote_shadow(self, model_id: str) -> None: ...

    def discard_shadow(self, model_id: str) -> None: ...


class ShadowModelController:
    """
    ACTIVATE promotes a shadow -- only when ``evaluate_shadow`` says it is
    ready; ``promote_shadow`` itself does not re-check, so this is the gate.
    STOP discards the shadow.
    """

    def __init__(self, models: _ModelRegistryLike) -> None:
        self._models = models

    @staticmethod
    def _model_id(record: ComponentRecord) -> str:
        return str(record.version.configuration.get("model_id", record.version.version))

    def perform(
        self,
        record: ComponentRecord,
        action: LifecycleAction,
        target_version: ComponentVersion | None,
    ) -> None:
        model_id = self._model_id(record)
        if action is LifecycleAction.ACTIVATE:
            ready, reason = self._models.evaluate_shadow(model_id)
            if not ready:
                raise PromotionRefusedError(reason)
            self._models.promote_shadow(model_id)
        elif action is LifecycleAction.STOP:
            self._models.discard_shadow(model_id)
        else:
            raise UnsupportedActionError(record.component_id, action, "model controller")

    def probe(self, record: ComponentRecord) -> HealthReport:
        model_id = self._model_id(record)
        if model_id == self._models.live_model_id:
            return HealthReport(HealthState.HEALTHY, "live")
        if model_id in self._models.shadow_ids():
            return HealthReport(HealthState.HEALTHY, "shadow")
        return HealthReport(HealthState.UNHEALTHY, "not in the model registry")


class ModelSupervisor(Supervisor):
    component_type = ComponentType.MODEL

    def _after_success(self, record: ComponentRecord, action: LifecycleAction, *, actor: str) -> None:
        # Promotion takes the live slot: whichever model held it is retired.
        if action is not LifecycleAction.ACTIVATE:
            return
        for other in self._registry.components(ComponentType.MODEL):
            if other.component_id != record.component_id and other.state is LifecycleState.ACTIVE:
                self._registry.observe(
                    other.component_id,
                    LifecycleState.STOPPED,
                    actor=actor,
                    health=HealthReport(
                        HealthState.UNKNOWN, f"superseded by {record.component_id}", other.updated_at
                    ),
                )


SUPERVISOR_TYPES: Mapping[ComponentType, type[Supervisor]] = {
    ComponentType.ENGINE: EngineSupervisor,
    ComponentType.STRATEGY: StrategySupervisor,
    ComponentType.MODEL: ModelSupervisor,
    ComponentType.TASK: TaskSupervisor,
    ComponentType.WORKER: WorkerSupervisor,
    ComponentType.PROVIDER: ProviderSupervisor,
    ComponentType.TUNING: TuningSupervisor,
    ComponentType.EVENTBUS: EventBusSupervisor,
    ComponentType.UPGRADE: UpgradeSupervisor,
}


class SupervisorSet:
    """One supervisor per component type; dispatches by the component's type."""

    def __init__(
        self,
        registry: RuntimeRegistry,
        controllers: Mapping[ComponentType, Controller] | None = None,
        *,
        policy: RestartPolicy | None = None,
        factory: Callable[[ComponentType], type[Supervisor]] = SUPERVISOR_TYPES.__getitem__,
    ) -> None:
        self._registry = registry
        given = dict(controllers or {})
        self._supervisors: dict[ComponentType, Supervisor] = {
            ctype: factory(ctype)(registry, given.get(ctype, ObserveOnlyController()), policy=policy)
            for ctype in ComponentType
        }

    def for_type(self, component_type: ComponentType) -> Supervisor:
        return self._supervisors[component_type]

    def for_component(self, component_id: str) -> Supervisor:
        return self._supervisors[self._registry.get(component_id).spec.component_type]
