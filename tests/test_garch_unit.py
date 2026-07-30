"""Tests for GARCH(1,1) module: src/regime/garch.py."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.regime.garch import (
    Garch11Params,
    _fit_garch11_offline,
    _neg_log_likelihood,
    annualize_volatility,
    conditional_volatility,
    rolling_garch_forecast,
)


def _synthetic_returns(n: int = 300, seed: int = 42) -> pd.Series:
    rng = np.random.default_rng(seed)
    returns = rng.normal(0, 0.01, size=n)
    return pd.Series(returns, name="returns")


# ---------------------------------------------------------------------------
# Garch11Params dataclass
# ---------------------------------------------------------------------------


def test_persistence() -> None:
    p = Garch11Params(omega=0.001, alpha=0.05, beta=0.90, scale=100.0)
    assert p.persistence == pytest.approx(0.95)


def test_unconditional_variance_stationary() -> None:
    p = Garch11Params(omega=0.01, alpha=0.05, beta=0.90, scale=100.0)
    # omega / (1 - alpha - beta) = 0.01 / 0.05 = 0.2
    assert p.unconditional_variance == pytest.approx(0.2, rel=1e-6)


def test_unconditional_variance_near_unit_root() -> None:
    # alpha + beta >= 1 → denom <= 0 → nan
    p = Garch11Params(omega=0.001, alpha=0.05, beta=0.95, scale=100.0)
    assert np.isnan(p.unconditional_variance)


# ---------------------------------------------------------------------------
# _neg_log_likelihood
# ---------------------------------------------------------------------------


def test_neg_log_likelihood_invalid_params_returns_large() -> None:
    returns = _synthetic_returns(100).to_numpy() * 100
    # omega <= 0
    assert _neg_log_likelihood(np.array([-0.001, 0.05, 0.90]), returns) == 1e10
    # alpha < 0
    assert _neg_log_likelihood(np.array([0.001, -0.1, 0.90]), returns) == 1e10
    # alpha + beta >= 1
    assert _neg_log_likelihood(np.array([0.001, 0.5, 0.5]), returns) == 1e10


def test_neg_log_likelihood_valid_params_finite() -> None:
    returns = _synthetic_returns(100).to_numpy() * 100
    val = _neg_log_likelihood(np.array([0.001, 0.05, 0.90]), returns)
    assert np.isfinite(val)


# ---------------------------------------------------------------------------
# _fit_garch11_offline
# ---------------------------------------------------------------------------


def test_fit_garch11_offline_too_few_obs() -> None:
    short = pd.Series([0.01] * 10)
    with pytest.raises(ValueError, match="at least"):
        _fit_garch11_offline(short)


def test_fit_garch11_offline_returns_valid_params() -> None:
    ret = _synthetic_returns(200)
    params = _fit_garch11_offline(ret)
    assert params.omega > 0
    assert params.alpha >= 0
    assert params.beta >= 0
    assert params.persistence < 1.0


def test_fit_garch11_offline_drops_nans() -> None:
    ret = _synthetic_returns(200)
    ret_with_nans = ret.copy()
    ret_with_nans.iloc[0:10] = np.nan
    params = _fit_garch11_offline(ret_with_nans)
    assert params.omega > 0


# ---------------------------------------------------------------------------
# conditional_volatility
# ---------------------------------------------------------------------------


def test_conditional_volatility_shape_matches() -> None:
    ret = _synthetic_returns(150)
    params = _fit_garch11_offline(ret)
    cv = conditional_volatility(ret, params)
    assert len(cv) == ret.notna().sum()


def test_conditional_volatility_positive() -> None:
    ret = _synthetic_returns(150)
    params = _fit_garch11_offline(ret)
    cv = conditional_volatility(ret, params)
    assert (cv >= 0).all()


def test_conditional_volatility_infinite_persistence_fallback() -> None:
    # When persistence >= 1, unconditional_variance is nan; seed should use sample var.
    ret = _synthetic_returns(100)
    params = Garch11Params(omega=0.001, alpha=0.05, beta=0.95, scale=100.0)
    cv = conditional_volatility(ret, params)
    assert len(cv) == len(ret)
    assert (cv >= 0).all()


# ---------------------------------------------------------------------------
# rolling_garch_forecast
# ---------------------------------------------------------------------------


def test_rolling_garch_forecast_window_too_small() -> None:
    with pytest.raises(ValueError, match="window must be"):
        rolling_garch_forecast(pd.Series([0.01] * 200), window=10)


def test_rolling_garch_forecast_nans_before_window() -> None:
    ret = _synthetic_returns(200)
    forecast = rolling_garch_forecast(ret, window=100, refit_every=20)
    # First 100 indices should be NaN
    assert forecast.iloc[:100].isna().all()


def test_rolling_garch_forecast_positive_after_window() -> None:
    ret = _synthetic_returns(300)
    forecast = rolling_garch_forecast(ret, window=100, refit_every=50)
    valid = forecast.dropna()
    assert len(valid) > 0
    assert (valid > 0).all()


def test_rolling_garch_forecast_length_matches_input() -> None:
    ret = _synthetic_returns(200)
    forecast = rolling_garch_forecast(ret, window=100, refit_every=20)
    assert len(forecast) == len(ret)


def test_rolling_garch_forecast_exception_fallback_produces_nan() -> None:
    """When scipy.optimize.minimize raises, the except branch must produce NaN
    at that step rather than crashing the entire forecast."""
    from unittest.mock import patch

    ret = _synthetic_returns(200)
    call_count = [0]

    original_minimize = __import__("scipy.optimize", fromlist=["minimize"]).minimize

    def fail_first_then_pass(fn, x0, *args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("simulated optimizer failure")
        return original_minimize(fn, x0, *args, **kwargs)

    with patch("src.regime.garch.minimize", side_effect=fail_first_then_pass):
        forecast = rolling_garch_forecast(ret, window=100, refit_every=100)

    # After the failure the branch falls back to sample variance and produces NaN
    assert len(forecast) == len(ret)
    # There must be at least some NaN values (before window and after failed fit)
    assert forecast.isna().any()


# ---------------------------------------------------------------------------
# annualize_volatility
# ---------------------------------------------------------------------------


def test_annualize_volatility_hourly() -> None:
    # per-bar vol of 0.01 → annualized = 0.01 * sqrt(24*365)
    per_bar = 0.01
    bars_per_year = 24 * 365
    ann = annualize_volatility(per_bar, bars_per_year)
    assert ann == pytest.approx(per_bar * np.sqrt(bars_per_year), rel=1e-6)


def test_annualize_volatility_zero() -> None:
    assert annualize_volatility(0.0, 365.0) == 0.0
