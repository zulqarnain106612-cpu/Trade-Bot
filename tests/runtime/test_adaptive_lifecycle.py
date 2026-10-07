"""RES-016: an adaptive candidate passes every stage in order, judged by
someone other than its proposer, reaches live only through an approved change,
and leaves it the way the post-promotion watchdog says."""

from __future__ import annotations

import pytest

from src.models.model_registry import ModelRegistry
from src.runtime.adaptive import (
    STAGE_ORDER,
    AdaptiveError,
    AdaptiveLifecycle,
    AdaptiveStage,
    Candidate,
    CandidateStatus,
    shadow_evidence,
)
from src.runtime.changes import Actor, ChangeError, ChangeStatus, UnauthorizedError
from src.runtime.contracts import (
    ChangeClass,
    ComponentType,
    HealthReport,
    HealthState,
    LifecycleAction,
    LifecycleState,
)
from src.tuning.watchdog import WatchdogOutcome

from ._support import AI, HUMAN, SYSTEM, Platform, build_platform, spec, version

A = LifecycleAction
S = LifecycleState


def _rec(
    life: AdaptiveLifecycle,
    candidate_id: str,
    stage: AdaptiveStage,
    evaluator: Actor = SYSTEM,
    *,
    passed: bool = True,
    detail: str = "ok",
) -> Candidate:
    return life.record(candidate_id, stage, passed=passed, detail=detail, evaluator=evaluator)


def _pass_all(life: AdaptiveLifecycle, candidate_id: str, evaluator: Actor = SYSTEM) -> None:
    for stage in STAGE_ORDER:
        _rec(life, candidate_id, stage, evaluator)


def test_proposals_must_name_a_version_change_or_an_activation(platform: Platform) -> None:
    life = AdaptiveLifecycle(platform.changes)
    with pytest.raises(AdaptiveError, match="replaces a version or activates"):
        life.propose("worker:w", A.STOP, AI, "x")
    with pytest.raises(AdaptiveError, match="needs a target version"):
        life.propose("worker:w", A.REPLACE, AI, "x")
    with pytest.raises(AdaptiveError, match="needs a target version"):
        life.propose("worker:w", A.ACTIVATE, AI, "x", version("2"))
    with pytest.raises(AdaptiveError, match="unknown candidate"):
        life.get("cand-none")


def test_stages_run_in_order_and_cannot_be_skipped(platform: Platform) -> None:
    life = AdaptiveLifecycle(platform.changes)
    c = life.propose("worker:w", A.REPLACE, AI, "tune", version("2"))
    assert c.next_stage is AdaptiveStage.BACKTEST
    with pytest.raises(AdaptiveError, match="needs BACKTEST next, not PROMOTION_GATE"):
        _rec(life, c.candidate_id, AdaptiveStage.PROMOTION_GATE)
    _pass_all(life, c.candidate_id)
    done = life.get(c.candidate_id)
    assert done.status is CandidateStatus.READY and done.next_stage is None
    with pytest.raises(AdaptiveError, match="is READY, not VALIDATING"):
        _rec(life, c.candidate_id, AdaptiveStage.BACKTEST)
    assert [e["stage"] for e in done.to_dict()["evidence"]] == [s.value for s in STAGE_ORDER]


def test_no_ai_or_proposer_judges_a_candidate(platform: Platform) -> None:
    life = AdaptiveLifecycle(platform.changes)
    c = life.propose("worker:w", A.REPLACE, SYSTEM, "self-tuned", version("2"))
    with pytest.raises(UnauthorizedError, match="proposed"):
        _rec(life, c.candidate_id, AdaptiveStage.BACKTEST)
    with pytest.raises(UnauthorizedError, match="cannot record BACKTEST"):
        _rec(life, c.candidate_id, AdaptiveStage.BACKTEST, AI)
    passed = _rec(life, c.candidate_id, AdaptiveStage.BACKTEST, HUMAN)
    assert passed.evidence[0].evaluator == "operator"


def test_a_failed_stage_rejects_the_candidate(platform: Platform) -> None:
    life = AdaptiveLifecycle(platform.changes)
    c = life.propose("worker:w", A.REPLACE, AI, "tune", version("2"))
    _rec(life, c.candidate_id, AdaptiveStage.BACKTEST)
    rejected = _rec(
        life, c.candidate_id, AdaptiveStage.STRESS, passed=False, detail="2008 replay -41%"
    )
    assert rejected.status is CandidateStatus.REJECTED
    assert rejected.outcome == "STRESS failed: 2008 replay -41%"
    with pytest.raises(AdaptiveError):
        life.request_promotion(c.candidate_id)


def test_a_model_reaches_live_only_through_human_approval_and_probation() -> None:
    p = build_platform()
    models = ModelRegistry(min_evaluations=1)
    models.register_shadow("m2")
    models.record_shadow_prediction("m2", 0.9, 1)
    models.record_live_prediction_for_comparison("m2", 0.1, 1)
    p.registry.register(spec("m2", ComponentType.MODEL), observed_state=S.STANDBY)
    life = AdaptiveLifecycle(p.changes, clock=p.clock)
    c = life.propose("model:m2", A.ACTIVATE, AI, "retrained on Q3")
    for stage in STAGE_ORDER:
        passed, detail = (
            shadow_evidence(models, "m2") if stage is AdaptiveStage.SHADOW else (True, "ok")
        )
        _rec(life, c.candidate_id, stage, passed=passed, detail=detail)
    change = life.request_promotion(c.candidate_id)
    assert change.status is ChangeStatus.AWAITING_APPROVAL
    assert change.classification is ChangeClass.SHADOW_REQUIRED
    with pytest.raises(ChangeError, match="needs STAGED"):
        life.on_change_approved(c.candidate_id)  # nobody approved yet
    assert p.controller.calls == []
    p.changes.approve(change.change_id, HUMAN)
    executed = life.on_change_approved(c.candidate_id)
    assert executed.status is ChangeStatus.EXECUTED
    assert executed.stages[0].detail.startswith("shadow accuracy 1.000 beats live")
    assert life.get(c.candidate_id).status is CandidateStatus.PROBATION
    probation = life.on_watchdog(c.candidate_id, WatchdogOutcome.IN_PROBATION)
    assert probation.status is CandidateStatus.PROBATION
    live = life.on_watchdog(c.candidate_id, WatchdogOutcome.CLEARED)
    assert (live.status, live.outcome) == (CandidateStatus.LIVE, "PROMOTED")
    assert life.candidates() == (live,)


def test_a_canary_class_change_carries_the_canary_evidence(platform: Platform) -> None:
    platform.registry.register(spec("trend", ComponentType.STRATEGY), observed_state=S.STANDBY)
    life = AdaptiveLifecycle(platform.changes)
    c = life.propose("strategy:trend", A.ACTIVATE, AI, "new strategy")
    _pass_all(life, c.candidate_id)
    change = life.request_promotion(c.candidate_id)
    assert change.classification is ChangeClass.CANARY_REQUIRED
    platform.changes.approve(change.change_id, HUMAN)
    executed = life.on_change_approved(c.candidate_id)
    assert executed.stages[0].stage == "canary"


def test_the_watchdog_rolls_a_tuned_version_back(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.ACTIVE)
    life = AdaptiveLifecycle(platform.changes)
    c = life.propose("worker:w", A.REPLACE, AI, "tune", version("2"))
    _pass_all(life, c.candidate_id, evaluator=HUMAN)
    change = life.request_promotion(c.candidate_id)
    assert change.classification is ChangeClass.LIVE_GATED
    platform.changes.approve(change.change_id, HUMAN)
    life.on_change_approved(c.candidate_id)
    assert platform.registry.get("worker:w").version.version == "2"
    rolled = life.on_watchdog(c.candidate_id, WatchdogOutcome.ROLLED_BACK)
    assert (rolled.status, rolled.outcome) == (CandidateStatus.ROLLED_BACK, "ROLLED_BACK")
    assert platform.registry.get("worker:w").version.version == "1"


def test_a_rejected_change_request_rejects_the_candidate(platform: Platform) -> None:
    life = AdaptiveLifecycle(platform.changes)
    c = life.propose("worker:missing", A.REPLACE, AI, "tune", version("2"))
    _pass_all(life, c.candidate_id)
    change = life.request_promotion(c.candidate_id)
    assert change.status is ChangeStatus.REJECTED
    after = life.get(c.candidate_id)
    assert after.status is CandidateStatus.REJECTED
    assert after.outcome == "change rejected: unknown component worker:missing"
    assert after.change_id == change.change_id


@pytest.mark.parametrize(
    ("unhealthy", "fails", "expected"),
    [(True, False, CandidateStatus.ROLLED_BACK), (False, True, CandidateStatus.FAILED)],
)
def test_execution_outcomes_map_to_candidate_states(
    unhealthy: bool, fails: bool, expected: CandidateStatus
) -> None:
    p = build_platform()
    p.registry.register(spec("w"), observed_state=S.ACTIVE)
    if unhealthy:
        p.controller.health = HealthReport(HealthState.UNHEALTHY, "errors")
    if fails:
        p.controller.fail_with = RuntimeError("swap failed")
    life = AdaptiveLifecycle(p.changes)
    c = life.propose("worker:w", A.REPLACE, AI, "tune", version("2"))
    _pass_all(life, c.candidate_id)
    change = life.request_promotion(c.candidate_id)
    p.changes.approve(change.change_id, HUMAN)
    life.on_change_approved(c.candidate_id)
    after = life.get(c.candidate_id)
    assert after.status is expected and after.outcome is not None
