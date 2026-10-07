"""GOV-070: every runtime mutation is a change request that is classified,
analysed, validated and policy-checked, and every step is audited."""

from __future__ import annotations

import pytest

from src.runtime.changes import (
    Actor,
    ActorKind,
    ChangeError,
    ChangeRequest,
    ChangeStatus,
    UnauthorizedError,
)
from src.runtime.contracts import ChangeClass, ComponentType, LifecycleAction, LifecycleState

from ._support import AI, HUMAN, NOBODY, REQUESTER, SYSTEM, Platform, spec, version

A = LifecycleAction
S = LifecycleState


def _request(cid: str, action: A, **kw: object) -> ChangeRequest:
    kw.setdefault("actor", HUMAN)
    kw.setdefault("reason", "because")
    return ChangeRequest(component_id=cid, action=action, **kw)  # type: ignore[arg-type]


def test_submit_never_executes(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.ACTIVE)
    change = platform.changes.submit(_request("worker:w", A.DRAIN))
    assert change.status is ChangeStatus.APPROVED
    assert change.classification is ChangeClass.LIVE_SAFE
    assert platform.controller.calls == []
    assert platform.registry.get("worker:w").state is S.ACTIVE


def test_live_safe_change_executes_and_promotes(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.ACTIVE)
    change = platform.changes.submit(_request("worker:w", A.DRAIN, actor=AI))
    done = platform.changes.execute(change.change_id, AI)
    assert done.status is ChangeStatus.PROMOTED
    record = platform.registry.get("worker:w")
    assert record.state is S.DRAINED
    assert record.desired is not None and record.desired.target_state is S.DRAINED
    assert record.desired.requested_by == "assistant"
    events = [e.event for e in platform.changes.audit_log(change.change_id)]
    assert events == ["submitted:APPROVED", "executed", "promoted"]
    assert len(platform.audit) == 3  # every entry reached the sink too


def test_gated_change_waits_for_approval_and_observation(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.STOPPED)
    change = platform.changes.submit(_request("worker:w", A.START, actor=REQUESTER))
    assert change.status is ChangeStatus.AWAITING_APPROVAL
    with pytest.raises(ChangeError, match="needs APPROVED"):
        platform.changes.execute(change.change_id, REQUESTER)
    platform.changes.approve(change.change_id, HUMAN)
    executed = platform.changes.execute(change.change_id, REQUESTER)
    assert executed.status is ChangeStatus.EXECUTED
    assert [e.event for e in platform.changes.audit_log(change.change_id)][-1] == "observing"
    promoted = platform.changes.promote(change.change_id, SYSTEM)
    assert promoted.status is ChangeStatus.PROMOTED and promoted.approved_by == "operator"
    assert promoted.to_dict()["status"] == "PROMOTED"


def test_unknown_component_and_missing_reason_are_rejected(platform: Platform) -> None:
    unknown = platform.changes.submit(_request("worker:nope", A.STOP))
    assert unknown.status is ChangeStatus.REJECTED
    assert unknown.reasons == ("unknown component worker:nope",)
    platform.registry.register(spec("w"), observed_state=S.ACTIVE)
    blank = platform.changes.submit(_request("worker:w", A.DRAIN, reason="  "))
    assert blank.reasons == ("a change needs a reason",)


def test_forbidden_and_invalid_changes_are_rejected_with_the_reason(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.ACTIVE)
    forbidden = platform.changes.submit(_request("worker:w", A.START))
    assert forbidden.status is ChangeStatus.REJECTED
    assert forbidden.classification is ChangeClass.FORBIDDEN
    assert "no lifecycle transition for START from ACTIVE" in forbidden.reasons[0]
    invalid = platform.changes.submit(_request("worker:w", A.REPLACE))
    assert invalid.status is ChangeStatus.REJECTED
    assert invalid.reasons == ("worker:w: REPLACE needs a new version",)


def test_stale_version_is_rejected(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.ACTIVE)
    stale = platform.changes.submit(_request("worker:w", A.DRAIN, expected_version="0"))
    assert stale.reasons == ("stale: requested against 0, component runs 1",)


def test_one_open_change_per_component(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.STOPPED)
    first = platform.changes.submit(_request("worker:w", A.START))
    second = platform.changes.submit(_request("worker:w", A.START))
    assert second.status is ChangeStatus.REJECTED
    assert second.reasons == (f"{first.change_id} is still open on worker:w",)
    assert platform.changes.open_change("worker:w") == first
    platform.changes.cancel(first.change_id, HUMAN)
    assert platform.changes.open_change("worker:w") is None


def test_resubmitting_is_idempotent_and_ids_cannot_be_reused(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.STOPPED)
    request = _request("worker:w", A.START, change_id="chg-fixed")
    assert platform.changes.submit(request) is platform.changes.submit(request)
    with pytest.raises(ChangeError, match="already names another request"):
        platform.changes.submit(_request("worker:w", A.STOP, change_id="chg-fixed"))
    assert len(platform.changes.changes("worker:w")) == 1
    assert len(platform.changes.changes()) == 1


def test_execution_detects_a_component_that_moved_after_approval(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.ACTIVE)
    change = platform.changes.submit(_request("worker:w", A.REPLACE, target_version=version("2")))
    platform.changes.approve(change.change_id, HUMAN)
    platform.registry.apply("worker:w", A.REPLACE, actor="elsewhere", target_version=version("3"))
    failed = platform.changes.execute(change.change_id, HUMAN)
    assert failed.status is ChangeStatus.FAILED
    assert failed.result == "component moved to 3 after approval"


def test_execution_failure_is_recorded(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.ACTIVE)
    change = platform.changes.submit(_request("worker:w", A.DRAIN))
    platform.controller.fail_with = RuntimeError("stuck")
    failed = platform.changes.execute(change.change_id, HUMAN)
    assert failed.status is ChangeStatus.FAILED
    assert "RuntimeError: stuck" in (failed.result or "")
    assert platform.registry.get("worker:w").state is S.FAILED


def test_cancel_rules(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.STOPPED)
    change = platform.changes.submit(_request("worker:w", A.START, actor=REQUESTER))
    with pytest.raises(UnauthorizedError, match="cannot cancel"):
        platform.changes.cancel(change.change_id, Actor("x", ActorKind.AI))
    cancelled = platform.changes.cancel(change.change_id, REQUESTER)
    assert cancelled.status is ChangeStatus.CANCELLED
    with pytest.raises(ChangeError, match="nothing to cancel"):
        platform.changes.cancel(change.change_id, HUMAN)


def test_unknown_change_ids(platform: Platform) -> None:
    with pytest.raises(ChangeError, match="unknown change"):
        platform.changes.get("chg-none")


def test_a_failing_audit_sink_does_not_lose_the_audit(platform: Platform) -> None:
    def broken(entry: object) -> None:
        raise OSError("sink down")

    platform.changes._sinks.append(broken)
    platform.registry.register(spec("w"), observed_state=S.ACTIVE)
    change = platform.changes.submit(_request("worker:w", A.DRAIN))
    first = platform.changes.audit_log(change.change_id)[0]
    assert first.to_dict()["event"] == "submitted:APPROVED"


def test_request_desired_records_intent_and_audits(platform: Platform) -> None:
    platform.registry.register(spec("m", ComponentType.MODEL), observed_state=S.STANDBY)
    desired = platform.changes.request_desired("model:m", S.ACTIVE, AI, "promote when approved")
    assert platform.registry.get("model:m").desired == desired
    assert platform.registry.get("model:m").state is S.STANDBY  # intent, not action
    (entry,) = platform.changes.audit_log("desired:model:m")
    assert entry.event == "desired_set" and "ACTIVE@current" in entry.detail
    with pytest.raises(UnauthorizedError, match="lacks runtime.request"):
        platform.changes.request_desired("model:m", S.STOPPED, NOBODY, "x")
