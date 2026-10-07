"""GOV-070: authorization -- an AI actor can request but never approve, record
a stage result or promote; every refusal is audited."""

from __future__ import annotations

import pytest

from src.runtime.changes import Actor, ChangeRequest, ChangeStatus, UnauthorizedError
from src.runtime.contracts import ComponentType, LifecycleAction, LifecycleState

from ._support import AI, HUMAN, NOBODY, REQUESTER, SYSTEM, Platform, spec

A = LifecycleAction
S = LifecycleState


def _gated(platform: Platform, actor: Actor = AI) -> str:
    platform.registry.register(spec("w"), observed_state=S.STOPPED)
    change = platform.changes.submit(ChangeRequest("worker:w", A.START, actor, "start it"))
    assert change.status is ChangeStatus.AWAITING_APPROVAL
    return change.change_id


def test_requests_need_the_requester_role(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.ACTIVE)
    change = platform.changes.submit(ChangeRequest("worker:w", A.DRAIN, NOBODY, "x"))
    assert change.status is ChangeStatus.REJECTED
    assert change.reasons == ("nobody lacks runtime.request",)


@pytest.mark.parametrize("approver", [AI, REQUESTER, SYSTEM])
def test_only_a_human_approver_approves(platform: Platform, approver: Actor) -> None:
    change_id = _gated(platform)
    with pytest.raises(UnauthorizedError, match="cannot approve"):
        platform.changes.approve(change_id, approver)
    assert platform.changes.get(change_id).status is ChangeStatus.AWAITING_APPROVAL
    assert platform.changes.audit_log(change_id)[-1].event == "approval_refused"


def test_an_ai_request_approved_by_a_human_can_be_executed_by_the_ai(platform: Platform) -> None:
    change_id = _gated(platform)
    platform.changes.approve(change_id, HUMAN)
    assert platform.changes.execute(change_id, AI).status is ChangeStatus.EXECUTED


def test_execution_needs_the_requester_role(platform: Platform) -> None:
    change_id = _gated(platform)
    platform.changes.approve(change_id, HUMAN)
    with pytest.raises(UnauthorizedError):
        platform.changes.execute(change_id, NOBODY)


def test_an_ai_cannot_promote_or_roll_back_without_the_role(platform: Platform) -> None:
    change_id = _gated(platform)
    platform.changes.approve(change_id, HUMAN)
    platform.changes.execute(change_id, AI)
    with pytest.raises(UnauthorizedError, match="cannot promote"):
        platform.changes.promote(change_id, AI)
    with pytest.raises(UnauthorizedError):
        platform.changes.rollback(change_id, NOBODY, "x")


def test_an_ai_cannot_record_its_own_shadow_result(platform: Platform) -> None:
    platform.registry.register(spec("m", ComponentType.MODEL), observed_state=S.STANDBY)
    change = platform.changes.submit(ChangeRequest("model:m", A.ACTIVATE, AI, "promote"))
    platform.changes.approve(change.change_id, HUMAN)
    with pytest.raises(UnauthorizedError, match="cannot record a shadow result"):
        platform.changes.record_stage(
            change.change_id, stage="shadow", passed=True, detail="trust me", actor=AI
        )
    assert platform.changes.audit_log(change.change_id)[-1].event == "stage_refused"
    assert platform.changes.get(change.change_id).status is ChangeStatus.APPROVED
