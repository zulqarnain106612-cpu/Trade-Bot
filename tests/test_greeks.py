"""Tests for the v5 Black-Scholes Greeks engine."""

from __future__ import annotations

import pytest

from src.risk.greeks import GreeksExposureCaps, check_greeks_within_caps, compute_greeks


def test_call_delta_between_zero_and_one() -> None:
    g = compute_greeks(spot=100, strike=100, time_to_expiry_years=0.5, volatility=0.3, is_call=True)
    assert 0.0 < g.delta < 1.0


def test_put_delta_between_minus_one_and_zero() -> None:
    g = compute_greeks(
        spot=100, strike=100, time_to_expiry_years=0.5, volatility=0.3, is_call=False
    )
    assert -1.0 < g.delta < 0.0


def test_deep_itm_call_delta_near_one() -> None:
    g = compute_greeks(spot=200, strike=100, time_to_expiry_years=0.1, volatility=0.2, is_call=True)
    assert g.delta > 0.95


def test_deep_otm_call_delta_near_zero() -> None:
    g = compute_greeks(spot=50, strike=100, time_to_expiry_years=0.1, volatility=0.2, is_call=True)
    assert g.delta < 0.05


def test_gamma_and_vega_positive() -> None:
    g = compute_greeks(spot=100, strike=100, time_to_expiry_years=0.5, volatility=0.3)
    assert g.gamma > 0
    assert g.vega > 0


def test_call_put_parity_delta_relationship() -> None:
    call = compute_greeks(
        spot=100, strike=100, time_to_expiry_years=0.5, volatility=0.3, is_call=True
    )
    put = compute_greeks(
        spot=100, strike=100, time_to_expiry_years=0.5, volatility=0.3, is_call=False
    )
    assert call.delta - put.delta == pytest.approx(1.0, abs=1e-6)


def test_rejects_nonpositive_spot() -> None:
    with pytest.raises(ValueError, match="spot"):
        compute_greeks(spot=0, strike=100, time_to_expiry_years=0.5, volatility=0.3)


def test_rejects_nonpositive_strike() -> None:
    with pytest.raises(ValueError, match="strike"):
        compute_greeks(spot=100, strike=0, time_to_expiry_years=0.5, volatility=0.3)


def test_rejects_nonpositive_time_to_expiry() -> None:
    with pytest.raises(ValueError, match="time_to_expiry_years"):
        compute_greeks(spot=100, strike=100, time_to_expiry_years=0, volatility=0.3)


def test_rejects_nonpositive_volatility() -> None:
    with pytest.raises(ValueError, match="volatility"):
        compute_greeks(spot=100, strike=100, time_to_expiry_years=0.5, volatility=0)


def test_check_greeks_within_caps_true() -> None:
    caps = GreeksExposureCaps(max_abs_delta=10.0, max_abs_vega=5.0)
    ok, reason = check_greeks_within_caps(5.0, 2.0, caps)
    assert ok
    assert reason == "within caps"


def test_check_greeks_exceeds_delta_cap() -> None:
    caps = GreeksExposureCaps(max_abs_delta=10.0, max_abs_vega=5.0)
    ok, reason = check_greeks_within_caps(15.0, 2.0, caps)
    assert not ok
    assert "delta" in reason


def test_check_greeks_exceeds_vega_cap() -> None:
    caps = GreeksExposureCaps(max_abs_delta=10.0, max_abs_vega=5.0)
    ok, reason = check_greeks_within_caps(5.0, 10.0, caps)
    assert not ok
    assert "vega" in reason
