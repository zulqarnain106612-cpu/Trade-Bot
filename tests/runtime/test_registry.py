"""GOV-067: the runtime registry answers what exists, at which version, in
which state, how healthy, depending on what, desired versus actual -- and
records only transitions the table and the capabilities allow."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from src.runtime.contracts import (
    ComponentType,
    DependencyDescriptor,
    DesiredState,
    HealthReport,
    HealthState,
    InvalidTransitionError,
    LifecycleAction,
    LifecycleState,
    RuntimeContractError,
    UnsupportedActionError,
)
from src.runtime.registry import RuntimeRegistry, UnknownComponentError

from ._support import COLD, Clock, spec, version

A = LifecycleAction
S = LifecycleState
NOW = datetime(2026, 10, 7, tzinfo=UTC)


@pytest.fixture
def registry() -> RuntimeRegistry:
    return RuntimeRegistry(clock=Clock())


def test_history_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="history_limit"):
        RuntimeRegistry(history_limit=0)


def test_register_records_discovery_and_observed_state(registry: RuntimeRegistry) -> None:
    record = registry.register(spec("w"), observed_state=S.ACTIVE, actor="adapter")
    assert record.state is S.ACTIVE
    (transition,) = registry.history("worker:w")
    assert transition.action is A.DISCOVER and transition.from_state is None
    assert transition.to_state is S.ACTIVE and transition.actor == "adapter"
    assert registry.get("worker:w") == record == registry.find("worker:w")


def test_unknown_components(registry: RuntimeRegistry) -> None:
    assert registry.find("worker:nope") is None
    with pytest.raises(UnknownComponentError):
        registry.get("worker:nope")


def test_components_filter_by_type_sorted(registry: RuntimeRegistry) -> None:
    registry.register(spec("b"))
    registry.register(spec("a"))
    registry.register(spec("m", ComponentType.MODEL))
    assert [r.component_id for r in registry.components()] == [
        "model:m",
        "worker:a",
        "worker:b",
    ]
    assert [r.component_id for r in registry.components(ComponentType.MODEL)] == ["model:m"]


def test_dependents_and_snapshot(registry: RuntimeRegistry) -> None:
    registry.register(spec("w"))
    registry.register(spec("t", ComponentType.TASK, deps=(DependencyDescriptor("worker:w"),)))
    assert registry.dependents("worker:w") == ("task:t",)
    snap = {row["component_id"]: row for row in registry.snapshot()}
    assert snap["worker:w"]["dependents"] == ["task:t"]
    assert snap["task:t"]["dependencies"][0]["component_id"] == "worker:w"


def test_apply_walks_the_lifecycle(registry: RuntimeRegistry) -> None:
    registry.register(spec("w"))
    for action, state in [
        (A.VALIDATE, S.VALIDATED),
        (A.INITIALIZE, S.INITIALIZED),
        (A.START, S.STANDBY),
        (A.ACTIVATE, S.ACTIVE),
        (A.PAUSE, S.PAUSED),
        (A.RESUME, S.ACTIVE),
        (A.DRAIN, S.DRAINED),
        (A.STOP, S.STOPPED),
    ]:
        t = registry.apply("worker:w", action, actor="op", change_id="c1")
        assert t.to_state is state and t.change_id == "c1"
    assert registry.get("worker:w").state is S.STOPPED
    assert len(registry.history("worker:w")) == 9
    assert len(registry.history()) == 9


def test_apply_refuses_before_changing_anything(registry: RuntimeRegistry) -> None:
    registry.register(spec("w"), observed_state=S.ACTIVE)
    before = registry.get("worker:w")
    with pytest.raises(InvalidTransitionError):
        registry.apply("worker:w", A.STOP, actor="op")
    with pytest.raises(RuntimeContractError, match="takes no version"):
        registry.apply("worker:w", A.PAUSE, actor="op", target_version=version("2"))
    assert registry.get("worker:w") == before


def test_apply_respects_capabilities(registry: RuntimeRegistry) -> None:
    registry.register(spec("e", ComponentType.ENGINE, caps=COLD), observed_state=S.ACTIVE)
    with pytest.raises(UnsupportedActionError):
        registry.apply("engine:e", A.REPLACE, actor="op", target_version=version("2"))


def test_preview_matches_apply_and_changes_nothing(registry: RuntimeRegistry) -> None:
    registry.register(spec("w"), observed_state=S.ACTIVE)
    before = registry.get("worker:w")
    state, v = registry.preview("worker:w", A.REPLACE, version("2"))
    assert (state, v.version) == (S.ACTIVE, "2")
    assert registry.get("worker:w") == before


def test_desired_state_is_separate_from_actual(registry: RuntimeRegistry) -> None:
    registry.register(spec("w"), observed_state=S.STANDBY)
    seen: list[tuple[str, object]] = []
    registry.add_desired_listener(lambda cid, d: seen.append((cid, d)))
    desired = DesiredState(S.ACTIVE, "op", "go live", NOW)
    record = registry.set_desired("worker:w", desired)
    assert record.desired == desired and record.state is S.STANDBY
    registry.set_desired("worker:w", None, notify=False)
    assert seen == [("worker:w", desired)]
    with pytest.raises(RuntimeContractError, match="not a version"):
        registry.set_desired("worker:w", DesiredState(S.ACTIVE, "op", "x", NOW, "9"))


def test_a_failing_desired_listener_does_not_undo_the_record(registry: RuntimeRegistry) -> None:
    registry.register(spec("w"))

    def broken(cid: str, desired: object) -> None:
        raise OSError("disk full")

    registry.add_desired_listener(broken)
    desired = DesiredState(S.STOPPED, "op", "x", NOW)
    assert registry.set_desired("worker:w", desired).desired == desired


def test_health_restart_and_failure_bookkeeping(registry: RuntimeRegistry) -> None:
    registry.register(spec("w"), observed_state=S.ACTIVE)
    report = HealthReport(HealthState.DEGRADED, "slow", NOW)
    assert registry.record_health("worker:w", report).health == report
    assert registry.note_restart("worker:w").restart_count == 1
    failed = registry.mark_failed("worker:w", A.RELOAD, "boom", actor="sup", change_id="c")
    assert failed.ok is False and failed.to_state is S.FAILED
    record = registry.get("worker:w")
    assert (record.state, record.failure_count, record.last_error) == (S.FAILED, 1, "boom")
    registry.apply("worker:w", A.STOP, actor="op")
    assert registry.get("worker:w").last_error is None


def test_observe_records_actual_state_without_an_action(registry: RuntimeRegistry) -> None:
    registry.register(spec("w"), observed_state=S.ACTIVE)
    unchanged = registry.observe("worker:w", S.ACTIVE, actor="adapter")
    assert unchanged == registry.get("worker:w")
    assert len(registry.history("worker:w")) == 1
    health = HealthReport(HealthState.UNKNOWN, "gone")
    record = registry.observe("worker:w", S.STOPPED, actor="adapter", health=health)
    assert record.state is S.STOPPED and record.health == health
    assert record.last_transition is not None and record.last_transition.action is None
    kept = registry.observe("worker:w", S.FAILED, actor="adapter")
    assert kept.health == health


def test_history_is_bounded() -> None:
    registry = RuntimeRegistry(clock=Clock(), history_limit=2)
    for name in "abc":
        registry.register(spec(name))
    assert [t.component_id for t in registry.history()] == ["worker:b", "worker:c"]


def test_concurrent_registration_of_one_id_admits_exactly_one(registry: RuntimeRegistry) -> None:
    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def register() -> None:
        barrier.wait()
        try:
            registry.register(spec("w"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=register) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(errors) == 7
    assert len(registry.components()) == 1
