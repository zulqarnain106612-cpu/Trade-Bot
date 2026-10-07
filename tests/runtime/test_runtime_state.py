"""GOV-068: the supervisor set dispatches by component type, components
without a controller are observe-only, and the registry snapshot reflects
every executed transition."""

from __future__ import annotations

import pytest

from src.runtime.contracts import ComponentType, LifecycleAction, LifecycleState
from src.runtime.registry import RuntimeRegistry
from src.runtime.supervisor import ActionFailedError, SupervisorSet, TaskSupervisor

from ._support import Clock, FakeController, Platform, spec

A = LifecycleAction
S = LifecycleState


def test_dispatch_by_component_type(platform: Platform) -> None:
    platform.registry.register(spec("t", ComponentType.TASK))
    assert isinstance(platform.supervisors.for_component("task:t"), TaskSupervisor)
    assert platform.supervisors.for_type(ComponentType.TASK) is platform.supervisors.for_component(
        "task:t"
    )


def test_types_without_a_controller_are_observe_only() -> None:
    registry = RuntimeRegistry(clock=Clock())
    registry.register(spec("w"), observed_state=S.STANDBY)
    supervisors = SupervisorSet(registry, {ComponentType.TASK: FakeController()})
    with pytest.raises(ActionFailedError, match="observe-only"):
        supervisors.for_component("worker:w").execute("worker:w", A.ACTIVATE, actor="op")
    assert registry.get("worker:w").state is S.FAILED  # the attempt is recorded, not hidden


def test_snapshot_tracks_actions_versions_and_health(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.STANDBY)
    sup = platform.supervisors.for_component("worker:w")
    sup.execute("worker:w", A.ACTIVATE, actor="op")
    sup.probe("worker:w")
    (row,) = platform.registry.snapshot()
    assert row["state"] == "ACTIVE"
    assert row["health"]["state"] == "HEALTHY"
    assert row["last_transition"]["action"] == "ACTIVATE"
    assert row["capabilities"]["supports_hot_swap"] is True
