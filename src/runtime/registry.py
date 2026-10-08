"""
The runtime registry -- one bookkeeping of record for every runtime component.

It does not replace ``StrategyRegistry``, ``models.ModelRegistry``,
``tuning.ParameterRegistry`` or ``upgrade.ModelRegistry``: those keep owning
their objects, and ``runtime.adapters`` reads them into ``ComponentSpec``s.
This registry owns only identity, version history, lifecycle state, desired
state, health and the transition history.

Every state change goes through ``apply`` (a lifecycle action the transition
table and the component's capabilities permit) or ``mark_failed``. The
registry performs nothing itself -- the supervisor calls the component's
controller first and records the outcome here -- so a record never claims a
state the component was not driven into.

Thread-safe: one re-entrant lock guards every read and write, and every
value handed out is an immutable snapshot.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

import structlog

from src.runtime.contracts import (
    ComponentRecord,
    ComponentSpec,
    ComponentType,
    ComponentVersion,
    DesiredState,
    HealthReport,
    LifecycleAction,
    LifecycleState,
    RuntimeContractError,
    TransitionRecord,
    next_state,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_HISTORY_LIMIT = 1000
# Observed version changes keep this many earlier versions per component.
_PREVIOUS_LIMIT = 50

DesiredListener = Callable[[str, DesiredState | None], None]
TransitionListener = Callable[[TransitionRecord], None]


class DuplicateComponentError(ValueError):
    """A component id is registered twice."""


class UnknownComponentError(KeyError):
    """No component with this id is registered."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RuntimeRegistry:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        history_limit: int = _HISTORY_LIMIT,
    ) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be >= 1")
        self._clock = clock
        self._lock = threading.RLock()
        self._records: dict[str, ComponentRecord] = {}
        self._history: deque[TransitionRecord] = deque(maxlen=history_limit)
        # Every version each component has ever run, by version string: a
        # version string names one content forever.
        self._versions: dict[str, dict[str, ComponentVersion]] = {}
        self._desired_listeners: list[DesiredListener] = []
        self._transition_listeners: list[TransitionListener] = []

    # -- registration --------------------------------------------------

    def register(
        self,
        spec: ComponentSpec,
        *,
        observed_state: LifecycleState = LifecycleState.DISCOVERED,
        actor: str = "discovery",
    ) -> ComponentRecord:
        """
        Add a component. ``observed_state`` lets an adapter record a component
        that is already running (a registered strategy is ACTIVE before the
        runtime platform ever saw it); the DISCOVER transition is recorded
        either way.
        """
        with self._lock:
            if spec.component_id in self._records:
                existing = self._records[spec.component_id].spec.version
                raise DuplicateComponentError(
                    f"{spec.component_id} already registered at version {existing.version}"
                )
            now = self._clock()
            next_state(spec.component_id, None, LifecycleAction.DISCOVER, spec.capabilities)
            transition = TransitionRecord(
                component_id=spec.component_id,
                action=LifecycleAction.DISCOVER,
                from_state=None,
                to_state=observed_state,
                from_version=None,
                to_version=spec.version.version,
                actor=actor,
                at=now,
            )
            record = ComponentRecord(
                spec=spec,
                state=observed_state,
                created_at=now,
                updated_at=now,
                last_transition=transition,
            )
            self._records[spec.component_id] = record
            self._versions[spec.component_id] = {spec.version.version: spec.version}
            self._remember(transition)
            return record

    def register_all(
        self, specs: Iterable[tuple[ComponentSpec, LifecycleState]], *, actor: str = "discovery"
    ) -> list[ComponentRecord]:
        """Register a batch atomically: a duplicate anywhere registers none."""
        batch = list(specs)
        with self._lock:
            ids = [spec.component_id for spec, _ in batch]
            clash = sorted(
                {i for i in ids if ids.count(i) > 1} | {i for i in ids if i in self._records}
            )
            if clash:
                raise DuplicateComponentError(f"duplicate component ids: {', '.join(clash)}")
            return [self.register(spec, observed_state=st, actor=actor) for spec, st in batch]

    # -- queries -------------------------------------------------------

    def get(self, component_id: str) -> ComponentRecord:
        with self._lock:
            try:
                return self._records[component_id]
            except KeyError:
                raise UnknownComponentError(component_id) from None

    def find(self, component_id: str) -> ComponentRecord | None:
        with self._lock:
            return self._records.get(component_id)

    def components(
        self, component_type: ComponentType | None = None
    ) -> tuple[ComponentRecord, ...]:
        with self._lock:
            return tuple(
                r
                for _, r in sorted(self._records.items())
                if component_type is None or r.spec.component_type is component_type
            )

    def dependents(self, component_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                cid
                for cid, r in sorted(self._records.items())
                if any(d.component_id == component_id for d in r.spec.dependencies)
            )

    def history(self, component_id: str | None = None) -> tuple[TransitionRecord, ...]:
        with self._lock:
            return tuple(
                t for t in self._history if component_id is None or t.component_id == component_id
            )

    def versions(self, component_id: str) -> tuple[ComponentVersion, ...]:
        """Every version the component has run, oldest first."""
        with self._lock:
            self.get(component_id)
            return tuple(self._versions[component_id].values())

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict(self.dependents(r.component_id)) for r in self.components()]

    # -- mutations -----------------------------------------------------

    def add_transition_listener(self, listener: TransitionListener) -> None:
        """Called after every recorded transition (event publishing hooks in here)."""
        with self._lock:
            self._transition_listeners.append(listener)

    def _remember(self, transition: TransitionRecord) -> None:
        self._history.append(transition)
        for listener in self._transition_listeners:
            try:
                listener(transition)
            except Exception as exc:  # a listener is a mirror; the record stands
                log.error(
                    "runtime.transition_listener_failed",
                    component_id=transition.component_id,
                    error=str(exc),
                )

    def add_desired_listener(self, listener: DesiredListener) -> None:
        """Called after every desired-state change (persistence hooks in here)."""
        with self._lock:
            self._desired_listeners.append(listener)

    def set_desired(
        self, component_id: str, desired: DesiredState | None, *, notify: bool = True
    ) -> ComponentRecord:
        """``notify=False`` when the value came from the store (restore)."""
        with self._lock:
            record = self.get(component_id)
            wanted = None if desired is None else desired.target_version
            if wanted is not None and wanted not in self._versions[component_id]:
                raise RuntimeContractError(
                    f"{component_id}: desired version {wanted} is not a "
                    f"version this component has run"
                )
            updated = self._store(record, desired=desired)
            if notify:
                for listener in self._desired_listeners:
                    try:
                        listener(component_id, desired)
                    except Exception as exc:  # a listener is a mirror; the record stands
                        log.error(
                            "runtime.desired_listener_failed",
                            component_id=component_id,
                            error=str(exc),
                        )
            return updated

    def record_health(self, component_id: str, report: HealthReport) -> ComponentRecord:
        with self._lock:
            return self._store(self.get(component_id), health=report)

    def apply(
        self,
        component_id: str,
        action: LifecycleAction,
        *,
        actor: str,
        target_version: ComponentVersion | None = None,
        change_id: str | None = None,
    ) -> TransitionRecord:
        """
        Record a lifecycle action the controller has performed. REPLACE needs
        a ``target_version`` different from the current one; ROLLBACK returns
        to the most recent previous version (or to ``target_version`` when it
        names one of them). Raises before changing anything.
        """
        with self._lock:
            record = self.get(component_id)
            to_state, spec, previous = self._plan(record, action, target_version)
            transition = TransitionRecord(
                component_id=component_id,
                action=action,
                from_state=record.state,
                to_state=to_state,
                from_version=record.version.version,
                to_version=spec.version.version,
                actor=actor,
                at=self._clock(),
                change_id=change_id,
            )
            self._versions[component_id][spec.version.version] = spec.version
            self._store(
                record,
                spec=spec,
                state=to_state,
                previous_versions=previous,
                last_transition=transition,
                last_error=None,
            )
            self._remember(transition)
            return transition

    def preview(
        self,
        component_id: str,
        action: LifecycleAction,
        target_version: ComponentVersion | None = None,
    ) -> tuple[LifecycleState, ComponentVersion]:
        """
        The (state, version) ``apply`` would record, or the exception it would
        raise -- without changing anything. The supervisor calls this before
        it asks a controller to act, so an illegal request never reaches one.
        """
        with self._lock:
            to_state, spec, _ = self._plan(self.get(component_id), action, target_version)
            return to_state, spec.version

    def _plan(
        self,
        record: ComponentRecord,
        action: LifecycleAction,
        target_version: ComponentVersion | None,
    ) -> tuple[LifecycleState, ComponentSpec, tuple[ComponentVersion, ...]]:
        component_id = record.component_id
        to_state = next_state(component_id, record.state, action, record.spec.capabilities)
        spec = record.spec
        previous = record.previous_versions
        if action is LifecycleAction.REPLACE:
            if target_version is None or target_version == spec.version:
                raise RuntimeContractError(f"{component_id}: REPLACE needs a new version")
            seen = self._versions[component_id].get(target_version.version)
            if seen is not None and seen != target_version:
                raise RuntimeContractError(
                    f"{component_id}: version {target_version.version} already names "
                    "different content; versions are immutable"
                )
            previous = (*previous, spec.version)
            spec = spec.with_version(target_version)
        elif action is LifecycleAction.ROLLBACK:
            if not previous:
                raise RuntimeContractError(f"{component_id}: no previous version to roll back to")
            target = previous[-1] if target_version is None else target_version
            if target not in previous:
                raise RuntimeContractError(
                    f"{component_id}: {target.version} is not a previous version"
                )
            index = previous.index(target)
            previous = previous[:index]
            spec = spec.with_version(target)
        elif target_version is not None:
            raise RuntimeContractError(f"{component_id}: {action.value} takes no version")
        return to_state, spec, previous

    def observe(
        self,
        component_id: str,
        state: LifecycleState,
        *,
        actor: str,
        health: HealthReport | None = None,
    ) -> ComponentRecord:
        """
        Record the actual state as read from the subsystem (an adapter
        re-sync, or a side effect of another component's action -- promoting
        a shadow model retires the old live one). Not a lifecycle action.
        """
        with self._lock:
            record = self.get(component_id)
            if record.state is state and health is None:
                return record
            transition = TransitionRecord(
                component_id=component_id,
                action=None,
                from_state=record.state,
                to_state=state,
                from_version=record.version.version,
                to_version=record.version.version,
                actor=actor,
                at=self._clock(),
            )
            self._remember(transition)
            return self._store(
                record,
                state=state,
                health=record.health if health is None else health,
                last_transition=transition,
            )

    def mark_failed(
        self,
        component_id: str,
        action: LifecycleAction,
        error: str,
        *,
        actor: str,
        change_id: str | None = None,
    ) -> TransitionRecord:
        """A controller raised while performing ``action``: the component is FAILED."""
        with self._lock:
            record = self.get(component_id)
            transition = TransitionRecord(
                component_id=component_id,
                action=action,
                from_state=record.state,
                to_state=LifecycleState.FAILED,
                from_version=record.version.version,
                to_version=record.version.version,
                actor=actor,
                at=self._clock(),
                ok=False,
                error=error,
                change_id=change_id,
            )
            self._store(
                record,
                state=LifecycleState.FAILED,
                last_transition=transition,
                last_error=error,
                failure_count=record.failure_count + 1,
            )
            self._remember(transition)
            return transition

    def resync(
        self,
        spec: ComponentSpec,
        state: LifecycleState,
        health: HealthReport,
        *,
        actor: str = "discovery",
    ) -> ComponentRecord:
        """
        Bring one record in line with what the component's owner reports now.

        New: registered at ``state``. Known: the owner's version (an observed
        change is recorded with the old version kept in ``previous_versions``),
        capabilities and dependencies replace the recorded ones, the state is
        observed and the health recorded. Every step is an observation -- the
        owner changed the component; the registry only records that it did.
        The observed content of a version string replaces any earlier content
        under the same string: the subsystem is the truth being recorded. The
        type cannot change: a spec's id carries its type (``ComponentSpec``).
        """
        with self._lock:
            component_id = spec.component_id
            record = self._records.get(component_id)
            if record is None:
                self.register(spec, observed_state=state, actor=actor)
                return self.record_health(component_id, health)
            if spec.version != record.version:
                transition = TransitionRecord(
                    component_id=component_id,
                    action=None,
                    from_state=record.state,
                    to_state=state,
                    from_version=record.version.version,
                    to_version=spec.version.version,
                    actor=actor,
                    at=self._clock(),
                )
                self._versions[component_id][spec.version.version] = spec.version
                previous = (*record.previous_versions, record.version)[-_PREVIOUS_LIMIT:]
                self._remember(transition)
                return self._store(
                    record,
                    spec=spec,
                    state=state,
                    health=health,
                    previous_versions=previous,
                    last_transition=transition,
                )
            if spec != record.spec:
                record = self._store(record, spec=spec)
            if record.state is not state:
                return self.observe(component_id, state, actor=actor, health=health)
            if health != record.health:
                return self._store(record, health=health)
            return record

    def retire(self, component_id: str) -> bool:
        """
        Drop a component its owner no longer reports. Only a STOPPED one that
        nothing depends on and that is not wanted in any other state: anything
        else is still someone's concern and stays visible. A desired state it
        carried is cleared through the listeners, so the stored row goes too.
        """
        with self._lock:
            record = self.get(component_id)
            if record.state is not LifecycleState.STOPPED or self.dependents(component_id):
                return False
            desired = record.desired
            if desired is not None and desired.target_state is not LifecycleState.STOPPED:
                return False
            del self._records[component_id]
            del self._versions[component_id]
            if desired is not None:
                for listener in self._desired_listeners:
                    try:
                        listener(component_id, None)
                    except Exception as exc:  # a listener is a mirror; the record stands
                        log.error(
                            "runtime.desired_listener_failed",
                            component_id=component_id,
                            error=str(exc),
                        )
            log.info("runtime.component_retired", component_id=component_id)
            return True

    def note_restart(self, component_id: str) -> ComponentRecord:
        with self._lock:
            record = self.get(component_id)
            return self._store(record, restart_count=record.restart_count + 1)

    def _store(self, record: ComponentRecord, **changes: Any) -> ComponentRecord:
        fields = {
            "spec": record.spec,
            "state": record.state,
            "created_at": record.created_at,
            "updated_at": self._clock(),
            "desired": record.desired,
            "health": record.health,
            "last_transition": record.last_transition,
            "last_error": record.last_error,
            "restart_count": record.restart_count,
            "failure_count": record.failure_count,
            "previous_versions": record.previous_versions,
        }
        fields.update(changes)
        updated = ComponentRecord(**fields)
        self._records[record.component_id] = updated
        return updated
