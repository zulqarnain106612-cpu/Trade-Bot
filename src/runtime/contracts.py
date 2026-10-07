"""
Runtime contracts: identity, immutable versions, capabilities, lifecycle.

Every runtime component is described by a ``ComponentSpec`` (what it is) and
tracked as a ``ComponentRecord`` (what it is doing). The two are kept apart on
purpose: the spec is immutable and identifies the component; the record is
the registry's view of its actual state, its desired state and its health,
and only the registry produces new records.

The lifecycle is a fixed transition table, ``TRANSITIONS``. An action is
legal when (a) the table has an edge for the component's current state and
(b) the component's ``CapabilitySet`` declares the action. Anything else is
refused with an exception that names which of the two failed -- a component
that cannot pause is never "paused" by bookkeeping alone.

Desired state and actual state are distinct types: ``DesiredState`` is a
request (target state, optional target version, who asked and why);
``LifecycleState`` on the record is what the supervisor last observed or
achieved.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class RuntimeContractError(ValueError):
    """A spec, version, capability set or desired state is malformed."""


class InvalidTransitionError(RuntimeError):
    """The lifecycle table has no edge for this state and action."""

    def __init__(self, state: LifecycleState | None, action: LifecycleAction) -> None:
        self.state = state
        self.action = action
        super().__init__(f"no lifecycle transition for {action.value} from {_name(state)}")


class UnsupportedActionError(RuntimeError):
    """The component's capabilities do not declare this action."""

    def __init__(self, component_id: str, action: LifecycleAction, reason: str) -> None:
        self.component_id = component_id
        self.action = action
        super().__init__(f"{component_id}: {action.value} not supported ({reason})")


class ComponentType(StrEnum):
    STRATEGY = "strategy"
    MODEL = "model"
    ENGINE = "engine"
    TUNING = "tuning"
    UPGRADE = "upgrade"
    WORKER = "worker"
    TASK = "task"
    PROVIDER = "provider"
    EVENTBUS = "eventbus"


class LifecycleState(StrEnum):
    DISCOVERED = "DISCOVERED"
    VALIDATED = "VALIDATED"
    INITIALIZED = "INITIALIZED"
    STANDBY = "STANDBY"  # running, not on the decision path
    ACTIVE = "ACTIVE"  # running and on the decision path
    PAUSED = "PAUSED"
    DRAINED = "DRAINED"  # accepts no new work; in-flight work finished
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


class LifecycleAction(StrEnum):
    DISCOVER = "DISCOVER"
    VALIDATE = "VALIDATE"
    INITIALIZE = "INITIALIZE"
    START = "START"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    RELOAD = "RELOAD"
    ACTIVATE = "ACTIVATE"
    DEACTIVATE = "DEACTIVATE"
    DRAIN = "DRAIN"
    STOP = "STOP"
    REPLACE = "REPLACE"
    ROLLBACK = "ROLLBACK"
    QUARANTINE = "QUARANTINE"


class ChangeClass(StrEnum):
    """How a runtime change may be rolled out, least to most restrictive."""

    LIVE_SAFE = "LIVE_SAFE"  # reduces exposure; no approval
    LIVE_GATED = "LIVE_GATED"  # live, after approval
    SHADOW_REQUIRED = "SHADOW_REQUIRED"  # only after a passed shadow stage
    CANARY_REQUIRED = "CANARY_REQUIRED"  # only after a passed canary stage
    DRAIN_REQUIRED = "DRAIN_REQUIRED"  # the component must be drained first
    RESTART_REQUIRED = "RESTART_REQUIRED"  # the component must be stopped first
    FORBIDDEN = "FORBIDDEN"


CHANGE_CLASS_ORDER: tuple[ChangeClass, ...] = tuple(ChangeClass)


class HealthState(StrEnum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"


S = LifecycleState
A = LifecycleAction

# Running states: the component has been started and has not been drained,
# stopped, failed or quarantined.
RUNNING_STATES: frozenset[LifecycleState] = frozenset({S.STANDBY, S.ACTIVE, S.PAUSED})

# States a version change can happen from without a restart (hot) and the
# states it can happen from cold (the component is not serving).
_HOT_SWAP_STATES = RUNNING_STATES
_COLD_SWAP_STATES: frozenset[LifecycleState] = frozenset({S.DRAINED, S.STOPPED, S.FAILED})


def _table() -> dict[tuple[LifecycleState | None, LifecycleAction], LifecycleState]:
    t: dict[tuple[LifecycleState | None, LifecycleAction], LifecycleState] = {
        (None, A.DISCOVER): S.DISCOVERED,
        (S.DISCOVERED, A.VALIDATE): S.VALIDATED,
        (S.VALIDATED, A.INITIALIZE): S.INITIALIZED,
        (S.INITIALIZED, A.START): S.STANDBY,
        (S.STOPPED, A.START): S.STANDBY,
        (S.STANDBY, A.ACTIVATE): S.ACTIVE,
        (S.ACTIVE, A.DEACTIVATE): S.STANDBY,
        (S.ACTIVE, A.PAUSE): S.PAUSED,
        (S.PAUSED, A.RESUME): S.ACTIVE,
    }
    for running in RUNNING_STATES:
        t[(running, A.RELOAD)] = running
        t[(running, A.DRAIN)] = S.DRAINED
    # An ACTIVE component is never stopped abruptly: it is deactivated or
    # drained first. Quarantine is the emergency path and is always open.
    for stoppable in (S.INITIALIZED, S.STANDBY, S.PAUSED, S.DRAINED, S.FAILED, S.QUARANTINED):
        t[(stoppable, A.STOP)] = S.STOPPED
    for swap in (A.REPLACE, A.ROLLBACK):
        for hot in _HOT_SWAP_STATES:
            t[(hot, swap)] = hot
        for cold in _COLD_SWAP_STATES:
            t[(cold, swap)] = S.INITIALIZED
    for state in LifecycleState:
        if state is not S.QUARANTINED:
            t[(state, A.QUARANTINE)] = S.QUARANTINED
    return t


TRANSITIONS: Mapping[tuple[LifecycleState | None, LifecycleAction], LifecycleState] = _table()

# Targets a desired state may name. Transient and bookkeeping states
# (DISCOVERED, VALIDATED, INITIALIZED, FAILED) are outcomes, not requests.
DESIRABLE_STATES: frozenset[LifecycleState] = frozenset(
    {S.STANDBY, S.ACTIVE, S.PAUSED, S.DRAINED, S.STOPPED, S.QUARANTINED}
)


def _name(state: LifecycleState | None) -> str:
    return "<unregistered>" if state is None else state.value


@dataclass(frozen=True, slots=True)
class CapabilitySet:
    """
    The lifecycle actions a component's controller can really perform.

    ``supports_hot_swap`` -- REPLACE/ROLLBACK may happen while running.
    ``requires_drain_for_replace`` -- REPLACE/ROLLBACK only from a drained,
    stopped or failed state. The two are mutually exclusive.
    """

    actions: frozenset[LifecycleAction] = frozenset()
    supports_hot_swap: bool = False
    requires_drain_for_replace: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.actions, frozenset) or not all(
            isinstance(a, LifecycleAction) for a in self.actions
        ):
            raise RuntimeContractError("actions must be a frozenset of LifecycleAction")
        if A.DISCOVER in self.actions:
            raise RuntimeContractError("DISCOVER is registry bookkeeping, not a capability")
        if (A.PAUSE in self.actions) != (A.RESUME in self.actions):
            raise RuntimeContractError("PAUSE and RESUME must be declared together")
        if self.supports_hot_swap and self.requires_drain_for_replace:
            raise RuntimeContractError(
                "supports_hot_swap and requires_drain_for_replace are mutually exclusive"
            )
        if self.supports_hot_swap and not (self.actions & {A.REPLACE, A.ROLLBACK}):
            raise RuntimeContractError("supports_hot_swap needs REPLACE or ROLLBACK")

    @property
    def supports_reload(self) -> bool:
        return A.RELOAD in self.actions

    @property
    def supports_pause(self) -> bool:
        return A.PAUSE in self.actions

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": sorted(a.value for a in self.actions),
            "supports_reload": self.supports_reload,
            "supports_pause": self.supports_pause,
            "supports_hot_swap": self.supports_hot_swap,
            "requires_drain_for_replace": self.requires_drain_for_replace,
        }


OBSERVE_ONLY = CapabilitySet()


def next_state(
    component_id: str,
    state: LifecycleState | None,
    action: LifecycleAction,
    capabilities: CapabilitySet,
) -> LifecycleState:
    """
    The state ``action`` leads to from ``state``, or an exception naming why
    it cannot happen. Pure: performs nothing.
    """
    target = TRANSITIONS.get((state, action))
    if target is None:
        raise InvalidTransitionError(state, action)
    if action is A.DISCOVER:
        return target
    if action not in capabilities.actions:
        raise UnsupportedActionError(component_id, action, "not declared")
    hot = action in (A.REPLACE, A.ROLLBACK) and state in _HOT_SWAP_STATES
    if hot and not capabilities.supports_hot_swap:
        raise UnsupportedActionError(
            component_id, action, f"no hot swap; drain or stop it first (state {_name(state)})"
        )
    return target


_ID_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]*$")


def make_component_id(component_type: ComponentType, name: str) -> str:
    """``<type>:<name>`` -- the only identity format the registry accepts."""
    if not _ID_PART.match(name):
        raise RuntimeContractError(f"invalid component name {name!r}")
    return f"{component_type.value}:{name}"


def _canonical_json(configuration: Mapping[str, Any]) -> str:
    try:
        return json.dumps(dict(configuration), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise RuntimeContractError(f"configuration is not JSON-serialisable: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ComponentVersion:
    """
    An immutable version identity. The configuration is held as canonical
    JSON so a caller holding the mapping it was built from cannot mutate the
    version afterwards; ``configuration`` returns a fresh copy each call.
    """

    version: str
    implementation: str
    configuration_json: str = "{}"

    def __post_init__(self) -> None:
        if not self.version or not isinstance(self.version, str):
            raise RuntimeContractError("version must be a non-empty string")
        if not self.implementation or not isinstance(self.implementation, str):
            raise RuntimeContractError("implementation must be a non-empty string")
        try:
            parsed = json.loads(self.configuration_json)
        except (TypeError, ValueError) as exc:
            raise RuntimeContractError("configuration_json is not JSON") from exc
        if not isinstance(parsed, dict) or _canonical_json(parsed) != self.configuration_json:
            raise RuntimeContractError("configuration_json must be a canonical JSON object")

    @classmethod
    def create(
        cls,
        version: str,
        implementation: str,
        configuration: Mapping[str, Any] | None = None,
    ) -> ComponentVersion:
        return cls(version, implementation, _canonical_json(configuration or {}))

    @property
    def configuration(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.configuration_json)
        return result

    @property
    def fingerprint(self) -> str:
        blob = json.dumps(
            [self.version, self.implementation, self.configuration_json], separators=(",", ":")
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "implementation": self.implementation,
            "configuration": self.configuration,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class DependencyDescriptor:
    """``component_id`` must exist; ``version``, when set, must match exactly."""

    component_id: str
    version: str | None = None
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "version": self.version,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    component_id: str
    component_type: ComponentType
    version: ComponentVersion
    owner: str
    capabilities: CapabilitySet = OBSERVE_ONLY
    dependencies: tuple[DependencyDescriptor, ...] = ()

    def __post_init__(self) -> None:
        prefix = f"{self.component_type.value}:"
        if not self.component_id.startswith(prefix):
            raise RuntimeContractError(
                f"component_id {self.component_id!r} must be '{prefix}<name>'"
            )
        make_component_id(self.component_type, self.component_id[len(prefix) :])
        if not self.owner:
            raise RuntimeContractError(f"{self.component_id}: owner must be set")
        if not isinstance(self.dependencies, tuple):
            raise RuntimeContractError(f"{self.component_id}: dependencies must be a tuple")
        seen: set[str] = set()
        for dep in self.dependencies:
            if dep.component_id == self.component_id:
                raise RuntimeContractError(f"{self.component_id}: depends on itself")
            if dep.component_id in seen:
                raise RuntimeContractError(
                    f"{self.component_id}: duplicate dependency {dep.component_id}"
                )
            seen.add(dep.component_id)

    def with_version(self, version: ComponentVersion) -> ComponentSpec:
        return ComponentSpec(
            component_id=self.component_id,
            component_type=self.component_type,
            version=version,
            owner=self.owner,
            capabilities=self.capabilities,
            dependencies=self.dependencies,
        )


@dataclass(frozen=True, slots=True)
class DesiredState:
    target_state: LifecycleState
    requested_by: str
    reason: str
    requested_at: datetime
    target_version: str | None = None

    def __post_init__(self) -> None:
        if self.target_state not in DESIRABLE_STATES:
            raise RuntimeContractError(
                f"{self.target_state.value} is an outcome, not a desirable state"
            )
        if not self.requested_by or not self.reason:
            raise RuntimeContractError("desired state needs requested_by and reason")
        if self.requested_at.tzinfo is None:
            raise RuntimeContractError("requested_at must be timezone-aware")

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_state": self.target_state.value,
            "target_version": self.target_version,
            "requested_by": self.requested_by,
            "reason": self.reason,
            "requested_at": self.requested_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class HealthReport:
    state: HealthState = HealthState.UNKNOWN
    detail: str = ""
    observed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "detail": self.detail,
            "observed_at": None if self.observed_at is None else self.observed_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """One lifecycle step. ``action`` is None for an observation: the actual
    state was read from the subsystem rather than driven by an action."""

    component_id: str
    action: LifecycleAction | None
    from_state: LifecycleState | None
    to_state: LifecycleState
    from_version: str | None
    to_version: str
    actor: str
    at: datetime
    ok: bool = True
    error: str | None = None
    change_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "action": "OBSERVED" if self.action is None else self.action.value,
            "from_state": None if self.from_state is None else self.from_state.value,
            "to_state": self.to_state.value,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "actor": self.actor,
            "at": self.at.isoformat(),
            "ok": self.ok,
            "error": self.error,
            "change_id": self.change_id,
        }


@dataclass(frozen=True, slots=True)
class ComponentRecord:
    """The registry's view of one component. Produced only by the registry."""

    spec: ComponentSpec
    state: LifecycleState
    created_at: datetime
    updated_at: datetime
    desired: DesiredState | None = None
    health: HealthReport = field(default_factory=HealthReport)
    last_transition: TransitionRecord | None = None
    last_error: str | None = None
    restart_count: int = 0
    failure_count: int = 0
    previous_versions: tuple[ComponentVersion, ...] = ()

    @property
    def component_id(self) -> str:
        return self.spec.component_id

    @property
    def version(self) -> ComponentVersion:
        return self.spec.version

    def to_dict(self, dependents: tuple[str, ...] = ()) -> dict[str, Any]:
        spec = self.spec
        return {
            "component_id": spec.component_id,
            "component_type": spec.component_type.value,
            "version": spec.version.version,
            "implementation": spec.version.implementation,
            "configuration": spec.version.configuration,
            "fingerprint": spec.version.fingerprint,
            "owner": spec.owner,
            "capabilities": spec.capabilities.to_dict(),
            "dependencies": [d.to_dict() for d in spec.dependencies],
            "dependents": list(dependents),
            "state": self.state.value,
            "desired_state": None if self.desired is None else self.desired.to_dict(),
            "health": self.health.to_dict(),
            "last_transition": (
                None if self.last_transition is None else self.last_transition.to_dict()
            ),
            "last_error": self.last_error,
            "restart_count": self.restart_count,
            "failure_count": self.failure_count,
            "previous_versions": [v.version for v in self.previous_versions],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
