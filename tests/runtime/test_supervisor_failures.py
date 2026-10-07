"""RES-011: a controller failure produces an explicit FAILED state; past the
restart budget the component is quarantined when it can be, and reported
unhealthy when it cannot -- never silently left looking healthy.

Decides: RES-011"""

from __future__ import annotations

import pytest

from src.runtime.contracts import (
    CapabilitySet,
    ComponentType,
    HealthState,
    LifecycleAction,
    LifecycleState,
)
from src.runtime.supervisor import (
    ActionFailedError,
    RestartBudgetExhaustedError,
    RestartPolicy,
    SupervisorError,
)

from ._support import ALL_ACTIONS, build_platform, spec

A = LifecycleAction
S = LifecycleState


def test_restart_policy_rejects_a_negative_budget() -> None:
    with pytest.raises(ValueError, match="max_restarts"):
        RestartPolicy(max_restarts=-1)


def test_controller_failure_marks_the_component_failed() -> None:
    p = build_platform()
    p.registry.register(spec("w"), observed_state=S.STANDBY)
    p.controller.fail_with = RuntimeError("socket closed")
    with pytest.raises(ActionFailedError) as err:
        p.supervisors.for_component("worker:w").execute("worker:w", A.ACTIVATE, actor="op")
    assert err.value.transition.ok is False
    assert err.value.transition.error == "RuntimeError: socket closed"
    record = p.registry.get("worker:w")
    assert (record.state, record.failure_count) == (S.FAILED, 1)
    assert record.last_error == "RuntimeError: socket closed"


def test_past_the_budget_a_quarantinable_component_is_quarantined() -> None:
    p = build_platform(max_restarts=0)
    p.registry.register(spec("w"), observed_state=S.STANDBY)
    p.controller.fail_with = RuntimeError("boom")
    p.controller.fail_on = frozenset({A.ACTIVATE})
    with pytest.raises(ActionFailedError):
        p.supervisors.for_component("worker:w").execute("worker:w", A.ACTIVATE, actor="op")
    assert p.registry.get("worker:w").state is S.QUARANTINED
    assert p.controller.calls[-1] == ("worker:w", A.QUARANTINE, None)


def test_past_the_budget_without_quarantine_it_stays_failed_and_unhealthy() -> None:
    p = build_platform(max_restarts=0)
    caps = CapabilitySet(ALL_ACTIONS - {A.QUARANTINE})
    p.registry.register(spec("w", caps=caps), observed_state=S.STANDBY)
    p.controller.fail_with = RuntimeError("boom")
    with pytest.raises(ActionFailedError):
        p.supervisors.for_component("worker:w").execute("worker:w", A.ACTIVATE, actor="op")
    record = p.registry.get("worker:w")
    assert record.state is S.FAILED
    assert record.health.state is HealthState.UNHEALTHY
    assert "quarantine not supported" in record.health.detail


def test_a_failed_quarantine_is_reported_not_hidden() -> None:
    p = build_platform(max_restarts=0)
    p.registry.register(spec("w"), observed_state=S.STANDBY)
    p.controller.fail_with = RuntimeError("wedged")
    with pytest.raises(ActionFailedError):
        p.supervisors.for_component("worker:w").execute("worker:w", A.ACTIVATE, actor="op")
    record = p.registry.get("worker:w")
    assert record.state is S.FAILED
    assert record.health.detail == "quarantine failed: RuntimeError: wedged"


def test_within_the_budget_nothing_else_happens() -> None:
    p = build_platform(max_restarts=1)
    p.registry.register(spec("w"), observed_state=S.STANDBY)
    p.controller.fail_with = RuntimeError("boom")
    with pytest.raises(ActionFailedError):
        p.supervisors.for_component("worker:w").execute("worker:w", A.ACTIVATE, actor="op")
    assert p.registry.get("worker:w").health.state is HealthState.UNKNOWN
    assert [c[1] for c in p.controller.calls] == [A.ACTIVATE]


def test_restart_is_stop_then_start_within_the_budget() -> None:
    p = build_platform(max_restarts=1)
    p.registry.register(spec("w"), observed_state=S.STANDBY)
    sup = p.supervisors.for_component("worker:w")
    with pytest.raises(SupervisorError, match="FAILED components"):
        sup.restart("worker:w", actor="op")
    p.controller.fail_with = RuntimeError("boom")
    with pytest.raises(ActionFailedError):
        sup.execute("worker:w", A.ACTIVATE, actor="op")
    p.controller.fail_with = None
    t = sup.restart("worker:w", actor="op", change_id="r1")
    assert (t.action, t.to_state, t.change_id) == (A.START, S.STANDBY, "r1")
    assert p.registry.get("worker:w").restart_count == 1
    p.controller.fail_with = RuntimeError("boom")
    with pytest.raises(ActionFailedError):
        sup.execute("worker:w", A.ACTIVATE, actor="op")
    with pytest.raises(RestartBudgetExhaustedError, match="1 restarts used of 1"):
        sup.restart("worker:w", actor="op")


def test_a_failing_probe_reports_unhealthy() -> None:
    p = build_platform()
    p.registry.register(spec("w"), observed_state=S.ACTIVE)
    p.registry.register(spec("v"), observed_state=S.ACTIVE)
    sup = p.supervisors.for_type(ComponentType.WORKER)
    assert sup.probe_all()["worker:v"].state is HealthState.HEALTHY
    p.controller.probe_error = TimeoutError("no answer")
    report = sup.probe("worker:w")
    assert report.state is HealthState.UNHEALTHY
    assert report.detail == "probe failed: TimeoutError: no answer"
    assert p.registry.get("worker:w").health == report
