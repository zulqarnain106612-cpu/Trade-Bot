"""GOV-071: mismatches between desired and actual state are detected and
classified; the reconciler acts only through the change manager's policy."""

from __future__ import annotations

from datetime import UTC, datetime

from src.runtime.changes import ChangeStatus
from src.runtime.contracts import (
    OBSERVE_ONLY,
    DesiredState,
    LifecycleAction,
    LifecycleState,
)
from src.runtime.reconcile import MismatchKind, Reconciler, diff, plan_path

from ._support import COLD, HOT, HUMAN, Platform, build_platform, spec, version

A = LifecycleAction
S = LifecycleState
NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _want(state: S, v: str | None = None) -> DesiredState:
    return DesiredState(state, "op", "intent", NOW, v)


def test_shortest_paths(platform: Platform) -> None:
    r = platform.registry
    r.register(spec("w"), observed_state=S.STOPPED)
    record = r.get("worker:w")
    assert plan_path(record, S.STOPPED) == ()
    assert plan_path(record, S.ACTIVE) == (A.START, A.ACTIVATE)
    assert plan_path(record, S.PAUSED) == (A.START, A.ACTIVATE, A.PAUSE)
    assert plan_path(record, S.QUARANTINED) == (A.QUARANTINE,)
    r.register(spec("e", caps=OBSERVE_ONLY), observed_state=S.ACTIVE)
    assert plan_path(r.get("worker:e"), S.STOPPED) is None


def test_diff_classifies_every_kind(platform: Platform) -> None:
    r = platform.registry
    for name, state in [("ok", S.ACTIVE), ("none", S.ACTIVE), ("st", S.STANDBY), ("f", S.FAILED)]:
        r.register(spec(name), observed_state=state)
    r.register(spec("obs", caps=OBSERVE_ONLY), observed_state=S.ACTIVE)
    r.register(spec("ver"), observed_state=S.ACTIVE)
    r.apply("worker:ver", A.REPLACE, actor="op", target_version=version("2"))
    r.set_desired("worker:ok", _want(S.ACTIVE))
    r.set_desired("worker:st", _want(S.ACTIVE))
    r.set_desired("worker:f", _want(S.STOPPED))
    r.set_desired("worker:obs", _want(S.STOPPED))
    r.set_desired("worker:ver", _want(S.ACTIVE, "1"))
    found = {m.component_id: m for m in diff(r)}
    assert set(found) == {"worker:st", "worker:f", "worker:obs", "worker:ver"}
    assert (found["worker:st"].kind, found["worker:st"].plan) == (MismatchKind.STATE, (A.ACTIVATE,))
    assert (found["worker:f"].kind, found["worker:f"].plan) == (MismatchKind.FAILED, (A.STOP,))
    assert found["worker:obs"].kind is MismatchKind.UNREACHABLE
    assert found["worker:obs"].plan == ()
    ver = found["worker:ver"]
    assert (ver.kind, ver.plan, ver.target_version) == (
        MismatchKind.VERSION,
        (A.ROLLBACK,),
        version("1"),
    )
    assert ver.detail == "worker:ver: desired ACTIVE@1, actual ACTIVE@2 (VERSION)"
    assert ver.to_dict()["plan"] == ["ROLLBACK"]


def test_a_version_rolled_back_from_is_reached_by_replace(platform: Platform) -> None:
    r = platform.registry
    r.register(spec("w"), observed_state=S.ACTIVE)
    r.apply("worker:w", A.REPLACE, actor="op", target_version=version("2"))
    r.apply("worker:w", A.ROLLBACK, actor="op")
    r.set_desired("worker:w", _want(S.ACTIVE, "2"))
    (mismatch,) = diff(r)
    assert mismatch.plan == (A.REPLACE,) and mismatch.target_version == version("2")


def test_risk_off_steps_execute_and_the_rest_wait_for_a_person(platform: Platform) -> None:
    r = platform.registry
    r.register(spec("down"), observed_state=S.ACTIVE)
    r.register(spec("up"), observed_state=S.STANDBY)
    r.set_desired("worker:down", _want(S.DRAINED))
    r.set_desired("worker:up", _want(S.ACTIVE))
    reconciler = Reconciler(r, platform.changes)
    outcomes = {o.mismatch.component_id: o for o in reconciler.reconcile_once()}
    assert outcomes["worker:down"].outcome == "executed: PROMOTED"
    assert r.get("worker:down").state is S.DRAINED
    assert outcomes["worker:up"].outcome == "awaiting approval"
    assert r.get("worker:up").state is S.STANDBY
    again = reconciler.reconcile_once()
    assert [o.outcome for o in again] == ["pending: AWAITING_APPROVAL"]
    change_id = again[0].change_id
    assert change_id is not None and again[0].to_dict()["change_id"] == change_id
    platform.changes.approve(change_id, HUMAN)
    platform.changes.execute(change_id, HUMAN)
    platform.changes.promote(change_id, HUMAN)
    assert reconciler.reconcile_once() == []


def test_unreachable_rejected_and_failed_steps_raise_alerts() -> None:
    p = build_platform()
    alerts: list[str] = []
    r = p.registry
    r.register(spec("obs", caps=OBSERVE_ONLY), observed_state=S.ACTIVE)
    r.register(spec("cold", caps=COLD), observed_state=S.ACTIVE)
    r.apply("worker:cold", A.DRAIN, actor="op")
    r.apply("worker:cold", A.REPLACE, actor="op", target_version=version("2"))
    r.apply("worker:cold", A.START, actor="op")
    r.register(spec("bad", caps=HOT), observed_state=S.ACTIVE)
    r.set_desired("worker:obs", _want(S.STOPPED))
    r.set_desired("worker:cold", _want(S.STANDBY, "1"))
    r.set_desired("worker:bad", _want(S.DRAINED))
    p.controller.fail_with = RuntimeError("drain hung")
    reconciler = Reconciler(r, p.changes, alert=alerts.append)
    outcomes = {o.mismatch.component_id: o.outcome for o in reconciler.reconcile_once()}
    assert outcomes == {
        "worker:obs": "alerted: unreachable",
        "worker:cold": "alerted: rejected",
        "worker:bad": f"executed: {ChangeStatus.FAILED.value}",
    }
    bad, cold, obs = alerts
    assert bad.startswith("DRAIN failed: worker:bad") and "drain hung" in bad
    assert cold.startswith("rejected ROLLBACK") and cold.endswith("drain it first")
    assert obs.startswith("unreachable: worker:obs")


def test_reconciler_without_an_alert_sink_still_logs(platform: Platform) -> None:
    platform.registry.register(spec("obs", caps=OBSERVE_ONLY), observed_state=S.ACTIVE)
    platform.registry.set_desired("worker:obs", _want(S.STOPPED))
    (outcome,) = Reconciler(platform.registry, platform.changes).reconcile_once()
    assert outcome.outcome == "alerted: unreachable"
