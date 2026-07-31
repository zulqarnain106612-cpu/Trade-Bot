"""Tests for src/strategies/funding_carry.py"""

from __future__ import annotations

import math

import pytest

from src.strategies.funding_carry import (
    evaluate_carry,
    is_carry_regime,
    suggested_notional,
)


# ---------------------------------------------------------------------------
# evaluate_carry — basic contracts
# ---------------------------------------------------------------------------


def test_evaluate_carry_zero_rate_not_tradeable():
    sig = evaluate_carry(0.0)
    assert sig.is_tradeable is False
    assert sig.direction == 0
    assert sig.carry_score == pytest.approx(0.0)


def test_evaluate_carry_high_positive_funding_tradeable():
    # 0.1% per 8h → gross APR = 0.1 * 3 * 365 = 109.5%
    sig = evaluate_carry(0.1)
    assert sig.is_tradeable is True
    assert sig.direction == 1  # short carry
    assert sig.annualised_apr_pct == pytest.approx(0.1 * 3 * 365)


def test_evaluate_carry_high_negative_funding_tradeable():
    sig = evaluate_carry(-0.1)
    assert sig.is_tradeable is True
    assert sig.direction == -1  # reverse carry


def test_evaluate_carry_below_min_apr_not_tradeable():
    # 0.001% per 8h → gross APR ≈ 1.095% → < default 10%
    sig = evaluate_carry(0.001, min_apr_pct=10.0)
    assert sig.is_tradeable is False
    assert "net_apr" in sig.reject_reason


def test_evaluate_carry_score_is_in_range():
    for rate in [-0.5, -0.1, -0.01, 0.0, 0.01, 0.1, 0.5]:
        sig = evaluate_carry(rate)
        assert -1.0 <= sig.carry_score <= 1.0


def test_evaluate_carry_score_positive_for_positive_rate():
    sig = evaluate_carry(0.05)
    assert sig.carry_score > 0.0


def test_evaluate_carry_score_negative_for_negative_rate():
    sig = evaluate_carry(-0.05)
    assert sig.carry_score < 0.0


def test_evaluate_carry_score_finite():
    sig = evaluate_carry(100.0)  # extreme value
    assert math.isfinite(sig.carry_score)
    assert abs(sig.carry_score) <= 1.0


def test_evaluate_carry_net_apr_less_than_gross():
    sig = evaluate_carry(0.1)
    assert sig.net_apr_pct < sig.annualised_apr_pct


def test_evaluate_carry_to_dict_keys():
    sig = evaluate_carry(0.1)
    d = sig.to_dict()
    assert "funding_rate_pct" in d
    assert "net_apr_pct" in d
    assert "carry_score" in d
    assert "is_tradeable" in d
    assert "direction" in d


def test_evaluate_carry_frozen():
    sig = evaluate_carry(0.1)
    with pytest.raises((AttributeError, TypeError)):
        sig.direction = 0  # type: ignore[misc]


def test_evaluate_carry_custom_min_apr():
    # At 0.05% per 8h: gross = 54.75%, net ≈ 54.65% > 5%
    sig = evaluate_carry(0.05, min_apr_pct=5.0)
    assert sig.is_tradeable is True


def test_evaluate_carry_fee_reduces_net_apr():
    sig_low_fee = evaluate_carry(0.01, fee_pct=0.0)
    sig_high_fee = evaluate_carry(0.01, fee_pct=1.0)
    assert sig_low_fee.net_apr_pct > sig_high_fee.net_apr_pct


# ---------------------------------------------------------------------------
# suggested_notional
# ---------------------------------------------------------------------------


def test_suggested_notional_zero_if_not_tradeable():
    sig = evaluate_carry(0.0)  # not tradeable
    assert suggested_notional(sig, capital_usd=100_000.0) == 0.0


def test_suggested_notional_zero_if_no_capital():
    sig = evaluate_carry(0.1)
    assert suggested_notional(sig, capital_usd=0.0) == 0.0


def test_suggested_notional_positive_for_tradeable():
    sig = evaluate_carry(0.1)
    n = suggested_notional(sig, capital_usd=100_000.0)
    assert n > 0.0


def test_suggested_notional_capped():
    sig = evaluate_carry(1.0)  # very high funding
    n = suggested_notional(sig, capital_usd=1_000_000.0, max_notional_usd=5_000.0)
    assert n <= 5_000.0


def test_suggested_notional_scales_with_capital():
    sig = evaluate_carry(0.1)
    n1 = suggested_notional(sig, capital_usd=10_000.0)
    n2 = suggested_notional(sig, capital_usd=100_000.0)
    assert n2 > n1


def test_suggested_notional_scales_with_risk_target():
    sig = evaluate_carry(0.1)
    n_low = suggested_notional(sig, capital_usd=100_000.0, risk_target_pct=0.5)
    n_high = suggested_notional(sig, capital_usd=100_000.0, risk_target_pct=2.0)
    assert n_high > n_low


# ---------------------------------------------------------------------------
# is_carry_regime
# ---------------------------------------------------------------------------


def test_carry_regime_safe():
    ok, reason = is_carry_regime(
        funding_rate_pct=0.05,
        futures_oi_change_pct=5.0,
        liquidation_pressure_zscore=0.5,
    )
    assert ok is True
    assert reason == ""


def test_carry_regime_low_funding():
    ok, reason = is_carry_regime(
        funding_rate_pct=0.001,
        futures_oi_change_pct=0.0,
        liquidation_pressure_zscore=0.0,
        min_funding_pct=0.005,
    )
    assert ok is False
    assert "funding_rate" in reason


def test_carry_regime_high_oi_change():
    ok, reason = is_carry_regime(
        funding_rate_pct=0.1,
        futures_oi_change_pct=25.0,
        liquidation_pressure_zscore=0.0,
    )
    assert ok is False
    assert "oi_change" in reason


def test_carry_regime_high_liquidation_pressure():
    ok, reason = is_carry_regime(
        funding_rate_pct=0.1,
        futures_oi_change_pct=5.0,
        liquidation_pressure_zscore=3.0,
    )
    assert ok is False
    assert "liquidation_zscore" in reason


def test_carry_regime_negative_funding_still_checked():
    ok, reason = is_carry_regime(
        funding_rate_pct=-0.1,
        futures_oi_change_pct=5.0,
        liquidation_pressure_zscore=0.0,
    )
    assert ok is True  # absolute value of funding passes the min threshold
