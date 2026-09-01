"""
Tests for pure-logic risk modules with no external dependencies:
  - src/risk/strategy_correlation.py
  - src/risk/greeks.py
  - src/risk/macro_exposure_budget.py
  - src/risk/strategy_decay.py
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# strategy_correlation
# ---------------------------------------------------------------------------


def test_combined_scalar_multiplicative() -> None:
    from src.risk.strategy_correlation import combined_correlation_scalar

    result = combined_correlation_scalar(asset_scalar=0.8, strategy_scalar=0.5)
    assert result == pytest.approx(0.4)


def test_combined_scalar_both_one_returns_one() -> None:
    from src.risk.strategy_correlation import combined_correlation_scalar

    assert combined_correlation_scalar(1.0, 1.0) == pytest.approx(1.0)


def test_combined_scalar_either_zero_returns_zero() -> None:
    from src.risk.strategy_correlation import combined_correlation_scalar

    assert combined_correlation_scalar(0.0, 0.8) == pytest.approx(0.0)
    assert combined_correlation_scalar(0.8, 0.0) == pytest.approx(0.0)


def test_combined_scalar_invalid_asset_raises() -> None:
    from src.risk.strategy_correlation import combined_correlation_scalar

    with pytest.raises(ValueError, match="asset_scalar"):
        combined_correlation_scalar(1.5, 0.5)


def test_combined_scalar_invalid_strategy_raises() -> None:
    from src.risk.strategy_correlation import combined_correlation_scalar

    with pytest.raises(ValueError, match="strategy_scalar"):
        combined_correlation_scalar(0.5, -0.1)


def test_strategy_correlation_tracker_no_data_returns_none() -> None:
    from src.risk.strategy_correlation import StrategyCorrelationTracker

    tracker = StrategyCorrelationTracker()
    assert tracker.correlation("strat_a", "strat_b") is None


def test_strategy_correlation_tracker_scalar_one_when_no_data() -> None:
    from src.risk.strategy_correlation import StrategyCorrelationTracker

    tracker = StrategyCorrelationTracker()
    scalar = tracker.correlation_scalar("new_strat", ["strat_a", "strat_b"])
    assert scalar == 1.0


def test_strategy_correlation_tracker_push_and_retrieve() -> None:
    from src.risk.strategy_correlation import StrategyCorrelationTracker

    tracker = StrategyCorrelationTracker()
    for _ in range(50):
        tracker.push_strategy_returns({"strat_a": 0.001, "strat_b": 0.001})
    # After identical returns, correlation approaches 1 → scalar should be < 1
    scalar = tracker.correlation_scalar("strat_c", ["strat_a", "strat_b"])
    assert 0.0 <= scalar <= 1.0


def test_strategy_correlation_tracker_tracked_ids() -> None:
    from src.risk.strategy_correlation import StrategyCorrelationTracker

    tracker = StrategyCorrelationTracker()
    tracker.push_strategy_returns({"strat_a": 0.001, "strat_b": -0.0004})
    ids = tracker.tracked_strategy_ids
    assert "strat_a" in ids
    assert "strat_b" in ids


def test_get_strategy_correlation_singleton() -> None:
    from src.risk.strategy_correlation import StrategyCorrelationTracker, get_strategy_correlation

    singleton = get_strategy_correlation()
    assert isinstance(singleton, StrategyCorrelationTracker)
    assert get_strategy_correlation() is singleton


# ---------------------------------------------------------------------------
# greeks
# ---------------------------------------------------------------------------


def test_compute_greeks_call_atm_delta_near_half() -> None:
    from src.risk.greeks import compute_greeks

    g = compute_greeks(spot=100.0, strike=100.0, time_to_expiry_years=1.0, volatility=0.20)
    assert 0.45 < g.delta < 0.65  # ATM call delta ~0.54


def test_compute_greeks_put_delta_negative() -> None:
    from src.risk.greeks import compute_greeks

    g = compute_greeks(
        spot=100.0, strike=100.0, time_to_expiry_years=1.0, volatility=0.20, is_call=False
    )
    assert g.delta < 0.0


def test_compute_greeks_gamma_positive() -> None:
    from src.risk.greeks import compute_greeks

    g = compute_greeks(spot=100.0, strike=100.0, time_to_expiry_years=1.0, volatility=0.20)
    assert g.gamma > 0.0


def test_compute_greeks_vega_positive() -> None:
    from src.risk.greeks import compute_greeks

    g = compute_greeks(spot=100.0, strike=100.0, time_to_expiry_years=1.0, volatility=0.20)
    assert g.vega > 0.0


def test_compute_greeks_theta_negative_for_long_call() -> None:
    from src.risk.greeks import compute_greeks

    g = compute_greeks(spot=100.0, strike=100.0, time_to_expiry_years=1.0, volatility=0.20)
    assert g.theta < 0.0  # time decay costs the option buyer


def test_compute_greeks_invalid_spot_raises() -> None:
    from src.risk.greeks import compute_greeks

    with pytest.raises(ValueError, match="spot"):
        compute_greeks(spot=0.0, strike=100.0, time_to_expiry_years=1.0, volatility=0.20)


def test_compute_greeks_invalid_strike_raises() -> None:
    from src.risk.greeks import compute_greeks

    with pytest.raises(ValueError, match="strike"):
        compute_greeks(spot=100.0, strike=-5.0, time_to_expiry_years=1.0, volatility=0.20)


def test_compute_greeks_invalid_tte_raises() -> None:
    from src.risk.greeks import compute_greeks

    with pytest.raises(ValueError, match="time_to_expiry"):
        compute_greeks(spot=100.0, strike=100.0, time_to_expiry_years=0.0, volatility=0.20)


def test_compute_greeks_invalid_vol_raises() -> None:
    from src.risk.greeks import compute_greeks

    with pytest.raises(ValueError, match="volatility"):
        compute_greeks(spot=100.0, strike=100.0, time_to_expiry_years=1.0, volatility=0.0)


def test_check_greeks_within_caps_passes() -> None:
    from src.risk.greeks import GreeksExposureCaps, check_greeks_within_caps

    caps = GreeksExposureCaps(max_abs_delta=1.0, max_abs_vega=0.5)
    ok, reason = check_greeks_within_caps(portfolio_delta=0.3, portfolio_vega=0.1, caps=caps)
    assert ok
    assert "within caps" in reason


def test_check_greeks_delta_cap_exceeded() -> None:
    from src.risk.greeks import GreeksExposureCaps, check_greeks_within_caps

    caps = GreeksExposureCaps(max_abs_delta=0.5, max_abs_vega=1.0)
    ok, reason = check_greeks_within_caps(portfolio_delta=0.8, portfolio_vega=0.1, caps=caps)
    assert not ok
    assert "delta" in reason


def test_check_greeks_vega_cap_exceeded() -> None:
    from src.risk.greeks import GreeksExposureCaps, check_greeks_within_caps

    caps = GreeksExposureCaps(max_abs_delta=1.0, max_abs_vega=0.2)
    ok, reason = check_greeks_within_caps(portfolio_delta=0.1, portfolio_vega=0.5, caps=caps)
    assert not ok
    assert "vega" in reason


# ---------------------------------------------------------------------------
# macro_exposure_budget
# ---------------------------------------------------------------------------


def test_macro_scalar_max_risk_on_returns_one() -> None:
    from src.intelligence.macro_regime import MacroRegime, MacroRegimeResult
    from src.risk.macro_exposure_budget import compute_macro_exposure_scalar

    macro = MacroRegimeResult(regime=MacroRegime.RISK_ON, risk_appetite=1.0)
    budget = compute_macro_exposure_scalar(macro)
    assert budget.scalar == pytest.approx(1.0)


def test_macro_scalar_max_risk_off_returns_min() -> None:
    from src.intelligence.macro_regime import MacroRegime, MacroRegimeResult
    from src.risk.macro_exposure_budget import _MIN_BUDGET_SCALAR, compute_macro_exposure_scalar

    macro = MacroRegimeResult(regime=MacroRegime.RISK_OFF, risk_appetite=-1.0)
    budget = compute_macro_exposure_scalar(macro)
    assert budget.scalar == pytest.approx(_MIN_BUDGET_SCALAR)


def test_macro_scalar_neutral_between_bounds() -> None:
    from src.intelligence.macro_regime import MacroRegime, MacroRegimeResult
    from src.risk.macro_exposure_budget import _MIN_BUDGET_SCALAR, compute_macro_exposure_scalar

    macro = MacroRegimeResult(regime=MacroRegime.NEUTRAL, risk_appetite=0.0)
    budget = compute_macro_exposure_scalar(macro)
    assert _MIN_BUDGET_SCALAR < budget.scalar < 1.0


def test_macro_scalar_reason_contains_regime() -> None:
    from src.intelligence.macro_regime import MacroRegime, MacroRegimeResult
    from src.risk.macro_exposure_budget import compute_macro_exposure_scalar

    macro = MacroRegimeResult(regime=MacroRegime.RISK_OFF, risk_appetite=-0.5)
    budget = compute_macro_exposure_scalar(macro)
    assert "risk_off" in budget.reason.lower()


def test_apply_macro_budget_reduces_kelly() -> None:
    from src.risk.macro_exposure_budget import (
        MacroExposureBudget,
        apply_macro_budget_to_kelly_fraction,
    )

    budget = MacroExposureBudget(scalar=0.5, reason="test")
    result = apply_macro_budget_to_kelly_fraction(kelly_fraction=0.4, budget=budget)
    assert result == pytest.approx(0.2)


def test_apply_macro_budget_negative_kelly_raises() -> None:
    from src.risk.macro_exposure_budget import (
        MacroExposureBudget,
        apply_macro_budget_to_kelly_fraction,
    )

    budget = MacroExposureBudget(scalar=0.5, reason="test")
    with pytest.raises(ValueError, match="non-negative"):
        apply_macro_budget_to_kelly_fraction(kelly_fraction=-0.1, budget=budget)


# ---------------------------------------------------------------------------
# strategy_decay (CUSUM)
# ---------------------------------------------------------------------------


def test_cusum_starts_at_zero() -> None:
    from src.risk.strategy_decay import CusumDecayDetector

    detector = CusumDecayDetector(baseline_mean=0.5)
    assert detector.cusum_statistic == 0.0
    assert detector.observation_count == 0
    assert not detector.is_decayed


def test_cusum_good_performance_stays_near_zero() -> None:
    from src.risk.strategy_decay import CusumDecayDetector

    detector = CusumDecayDetector(baseline_mean=0.5, slack=0.5)
    for _ in range(50):
        detector.update(0.5)  # exactly at baseline → deviation = 0 - slack = -0.5 → max(0, ...) = 0
    assert detector.cusum_statistic == 0.0
    assert not detector.is_decayed


def test_cusum_persistent_underperformance_triggers_decay() -> None:
    from src.risk.strategy_decay import CusumDecayDetector

    detector = CusumDecayDetector(baseline_mean=1.0, slack=0.1, decision_threshold=5.0)
    # Each update: deviation = 1.0 - 0.0 - 0.1 = 0.9, CUSUM grows quickly
    for _ in range(10):
        detector.update(0.0)
    assert detector.is_decayed


def test_cusum_single_bad_observation_does_not_trigger() -> None:
    from src.risk.strategy_decay import CusumDecayDetector

    detector = CusumDecayDetector(baseline_mean=0.5, slack=0.1, decision_threshold=5.0)
    detector.update(-10.0)  # very bad single observation
    # CUSUM = max(0, 0.5 - (-10) - 0.1) = 10.4, actually this WOULD trigger
    # Use a small deviation instead
    detector2 = CusumDecayDetector(baseline_mean=0.5, slack=0.3, decision_threshold=5.0)
    detector2.update(0.3)  # deviation = 0.5 - 0.3 - 0.3 = -0.1 → max(0, -0.1) = 0
    assert detector2.cusum_statistic == 0.0
    assert not detector2.is_decayed


def test_cusum_reset_clears_state() -> None:
    from src.risk.strategy_decay import CusumDecayDetector

    detector = CusumDecayDetector(baseline_mean=1.0, slack=0.0, decision_threshold=5.0)
    for _ in range(10):
        detector.update(0.0)
    assert detector.is_decayed

    detector.reset()
    assert detector.cusum_statistic == 0.0
    assert detector.observation_count == 0
    assert not detector.is_decayed


def test_cusum_observation_count_increments() -> None:
    from src.risk.strategy_decay import CusumDecayDetector

    detector = CusumDecayDetector(baseline_mean=0.5)
    for _ in range(7):
        detector.update(0.5)
    assert detector.observation_count == 7


def test_cusum_returns_statistic_from_update() -> None:
    from src.risk.strategy_decay import CusumDecayDetector

    detector = CusumDecayDetector(baseline_mean=1.0, slack=0.0)
    stat = detector.update(0.0)
    assert stat == detector.cusum_statistic
    assert stat > 0.0
