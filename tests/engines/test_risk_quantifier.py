"""Tests for risk quantifier and MAE estimate."""

from src.engines.risk_quantifier import (
    RiskQuantifier,
    mae_estimate,
    tail_risk_score,
    uncertainty_score,
)


def test_uncertainty_high_confidence():
    _, label = uncertainty_score(49_900.0, 50_100.0, 50_000.0)
    assert label == "high_confidence"


def test_uncertainty_suppress_wide_ci():
    _, label = uncertainty_score(45_000.0, 55_000.0, 50_000.0)
    assert label == "suppress"


def test_uncertainty_moderate():
    _, label = uncertainty_score(49_000.0, 51_000.0, 50_000.0)
    assert label == "moderate"


def test_tail_risk_high_jump_low_liq():
    score = tail_risk_score(jump_prob=0.5, liquidity_score=0.1)
    assert score > 0.3


def test_tail_risk_zero_jump():
    score = tail_risk_score(jump_prob=0.0, liquidity_score=0.9)
    assert score == 0.0


def test_mae_estimate_positive():
    mae = mae_estimate(50_000.0, 0.8, 4)
    assert mae > 0


def test_risk_quantifier_returns_dict():
    rq = RiskQuantifier()
    result = rq.quantify(
        ci_low=49_500.0,
        ci_high=50_500.0,
        consensus=50_000.0,
        jump_prob=0.02,
        liquidity_score=0.8,
        yz_vol=0.5,
        horizon_hours=4,
    )
    assert "uncertainty_label" in result
    assert "tail_risk_score" in result
    assert "mae_99" in result
    assert result["mae_99"] > 0
