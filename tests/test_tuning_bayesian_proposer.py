from pathlib import Path

import pytest

from src.tuning.audit import TuningAuditLog, TuningEventType
from src.tuning.bayesian_proposer import BayesianProposer
from src.tuning.registry import TunableParameter


def make_param(current: float = 0.5, floor: float = 0.3, ceiling: float = 0.7) -> TunableParameter:
    return TunableParameter(
        name="hmm.entropy_threshold",
        description="test",
        floor=floor,
        ceiling=ceiling,
        current=current,
        eval_strategy="cpcv_oos_sharpe",
    )


def test_propose_stays_within_bounds_cold_start(tmp_path: Path) -> None:
    audit_log = TuningAuditLog(tmp_path / "audit.jsonl")
    proposer = BayesianProposer(audit_log, seed=42)
    param = make_param()
    for _ in range(20):
        proposal = proposer.propose(param, primary_metric="oos_sharpe")
        assert param.floor <= proposal.challenger_value <= param.ceiling


def test_propose_is_deterministic_with_seeded_sampler(tmp_path: Path) -> None:
    audit_log = TuningAuditLog(tmp_path / "audit.jsonl")
    param = make_param()
    proposer1 = BayesianProposer(audit_log, seed=7)
    proposer2 = BayesianProposer(audit_log, seed=7)
    assert (
        proposer1.propose(param, primary_metric="oos_sharpe").challenger_value
        == proposer2.propose(param, primary_metric="oos_sharpe").challenger_value
    )


def test_propose_records_champion_value(tmp_path: Path) -> None:
    audit_log = TuningAuditLog(tmp_path / "audit.jsonl")
    proposer = BayesianProposer(audit_log, seed=1)
    param = make_param(current=0.55)
    proposal = proposer.propose(param, primary_metric="oos_sharpe")
    assert proposal.champion_value == 0.55
    assert proposal.param_name == "hmm.entropy_threshold"


def test_propose_without_primary_metric_falls_back_to_cold_start(tmp_path: Path) -> None:
    audit_log = TuningAuditLog(tmp_path / "audit.jsonl")
    proposer = BayesianProposer(audit_log, seed=3)
    param = make_param()
    proposal = proposer.propose(param)
    assert param.floor <= proposal.challenger_value <= param.ceiling


def test_propose_uses_audit_log_history_to_favor_improving_region(tmp_path: Path) -> None:
    audit_log = TuningAuditLog(tmp_path / "audit.jsonl")
    param = make_param(current=0.5, floor=0.0, ceiling=1.0)

    # Simulate a history where values near 0.9 strongly improved oos_sharpe,
    # and values near 0.1 strongly regressed it.
    for value, delta in [(0.9, 1.0), (0.85, 0.9), (0.1, -1.0), (0.15, -0.9)]:
        audit_log.record(param.name, TuningEventType.PROPOSED, {"challenger_value": value})
        audit_log.record(
            param.name,
            TuningEventType.EVALUATED,
            {"comparisons": [{"metric": "oos_sharpe", "delta": delta, "p_value": 0.01}]},
        )

    proposer = BayesianProposer(audit_log, n_startup_trials=1, seed=0)
    proposal = proposer.propose(param, primary_metric="oos_sharpe")
    assert proposal.challenger_value > 0.5


def test_propose_zero_range_width_returns_step_pct_zero(tmp_path: Path) -> None:
    """When floor == ceiling the step_pct guard returns 0.0 without dividing by zero."""
    audit_log = TuningAuditLog(tmp_path / "audit.jsonl")
    proposer = BayesianProposer(audit_log, seed=99)
    param = make_param(current=0.5, floor=0.5, ceiling=0.5)
    proposal = proposer.propose(param, primary_metric="oos_sharpe")
    assert proposal.step_pct == 0.0
    assert proposal.challenger_value == pytest.approx(0.5)


def test_propose_ignores_evaluations_with_unrelated_metric(tmp_path: Path) -> None:
    audit_log = TuningAuditLog(tmp_path / "audit.jsonl")
    param = make_param()
    audit_log.record(param.name, TuningEventType.PROPOSED, {"challenger_value": 0.6})
    audit_log.record(
        param.name,
        TuningEventType.EVALUATED,
        {
            "comparisons": [
                {"metric": "slippage_prediction_accuracy", "delta": 0.5, "p_value": 0.01}
            ]
        },
    )
    proposer = BayesianProposer(audit_log, seed=2)
    proposal = proposer.propose(param, primary_metric="oos_sharpe")
    assert param.floor <= proposal.challenger_value <= param.ceiling
