"""
Shared helpers for the runtime-platform suite (imported by the test modules;
``conftest.py`` turns ``build_platform`` into the ``platform`` fixture).

Everything is in memory: a stepping clock instead of wall time, a recording
controller instead of a subsystem, so every lifecycle path runs in
microseconds and deterministically.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from src.runtime.changes import (
    ROLE_APPROVER,
    ROLE_REQUESTER,
    Actor,
    ActorKind,
    ChangeManager,
)
from src.runtime.contracts import (
    CapabilitySet,
    ComponentRecord,
    ComponentSpec,
    ComponentType,
    ComponentVersion,
    DependencyDescriptor,
    HealthReport,
    HealthState,
    LifecycleAction,
    LifecycleState,
)
from src.runtime.registry import RuntimeRegistry
from src.runtime.supervisor import RestartPolicy, SupervisorSet

A = LifecycleAction
S = LifecycleState

ALL_ACTIONS = frozenset(a for a in LifecycleAction if a is not A.DISCOVER)
HOT = CapabilitySet(ALL_ACTIONS, supports_hot_swap=True, requires_drain_for_replace=False)
COLD = CapabilitySet(ALL_ACTIONS)

HUMAN = Actor("operator", ActorKind.HUMAN, frozenset({ROLE_REQUESTER, ROLE_APPROVER}))
REQUESTER = Actor("requester", ActorKind.HUMAN, frozenset({ROLE_REQUESTER}))
AI = Actor("assistant", ActorKind.AI, frozenset({ROLE_REQUESTER, ROLE_APPROVER}))
SYSTEM = Actor("watchdog", ActorKind.SYSTEM, frozenset({ROLE_REQUESTER}))
NOBODY = Actor("nobody", ActorKind.HUMAN, frozenset())


class Clock:
    """Advances one second per reading: every timestamp is distinct and ordered."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 7, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def version(v: str = "1", impl: str = "pkg.Impl", **config: object) -> ComponentVersion:
    return ComponentVersion.create(v, impl, config)


def spec(
    name: str = "w",
    ctype: ComponentType = ComponentType.WORKER,
    *,
    v: str = "1",
    caps: CapabilitySet = HOT,
    deps: Iterable[DependencyDescriptor] = (),
) -> ComponentSpec:
    return ComponentSpec(f"{ctype.value}:{name}", ctype, version(v), "tests", caps, tuple(deps))


@dataclass
class FakeController:
    """Records every call; raises ``fail_with`` when set; probes ``health``."""

    calls: list[tuple[str, LifecycleAction, str | None]] = field(default_factory=list)
    fail_with: BaseException | None = None
    fail_on: frozenset[LifecycleAction] | None = None
    health: HealthReport = field(default_factory=lambda: HealthReport(HealthState.HEALTHY, "ok"))
    probe_error: Exception | None = None

    def perform(
        self,
        record: ComponentRecord,
        action: LifecycleAction,
        target_version: ComponentVersion | None,
    ) -> None:
        shown = None if target_version is None else target_version.version
        self.calls.append((record.component_id, action, shown))
        if self.fail_with is not None and (self.fail_on is None or action in self.fail_on):
            raise self.fail_with

    def probe(self, record: ComponentRecord) -> HealthReport:
        if self.probe_error is not None:
            raise self.probe_error
        return self.health


@dataclass
class Platform:
    clock: Clock
    registry: RuntimeRegistry
    controller: FakeController
    supervisors: SupervisorSet
    changes: ChangeManager
    audit: list[object]


def build_platform(max_restarts: int = 3) -> Platform:
    clock = Clock()
    registry = RuntimeRegistry(clock=clock)
    controller = FakeController()
    supervisors = SupervisorSet(
        registry,
        {t: controller for t in ComponentType},
        policy=RestartPolicy(max_restarts=max_restarts),
    )
    audit: list[object] = []
    changes = ChangeManager(registry, supervisors, clock=clock, audit_sinks=[audit.append])
    return Platform(clock, registry, controller, supervisors, changes, audit)
