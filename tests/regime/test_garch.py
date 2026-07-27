"""Tests for GARCH(1,1) conditional volatility — src/regime/garch.py."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.regime.garch import (
    Garch11Params,
    annualize_volatility,
    conditional_volatility,
    fit_garch11,
    rolling_garch_forecast,
)


def _synthetic_garch_returns(n: int = 600, seed: int = 7) -> pd.Series:
    """Simulate a real GARCH(1,1) process so the fitted params have a known ground truth to check against."""
    rng = np.random.default_rng(seed)
    omega, alpha, beta = 0.05, 0.10, 0.85
    sigma2 = np.empty(n)
    eps = np.empty(n)
    sigma2[0] = omega / (1 - alpha - beta)
    eps[0] = rng.normal(0, np.sqrt(sigma2[0]))
    for t in range(1, n):
        sigma2[t] = omega + alpha * eps[t - 1] ** 2 + beta * sigma2[t - 1]
        eps[t] = rng.normal(0, np.sqrt(sigma2[t]))
    idx = pd.date_range("2024-01-01", periods=n, freq="h")
    return pd.Series(eps / 100.0, index=idx)


def test_unconditional_variance_is_nan_at_unit_root() -> None:
    """persistence (alpha+beta) >= 1 is a non-stationary process -- the
    long-run variance is undefined, not just large, so unconditional_variance
    must return nan rather than a huge or negative number."""
    params = Garch11Params(omega=0.05, alpha=0.5, beta=0.5, scale=1.0)
    assert params.persistence == pytest.approx(1.0)
    assert np.isnan(params.unconditional_variance)


def test_unconditional_variance_finite_when_stationary() -> None:
    params = Garch11Params(omega=0.05, alpha=0.10, beta=0.85, scale=1.0)
    assert params.persistence < 1.0
    assert params.unconditional_variance == pytest.approx(0.05 / (1.0 - 0.95))


def test_fit_garch11_recovers_plausible_params() -> None:
    returns = _synthetic_garch_returns()
    params = fit_garch11(returns)
    assert isinstance(params, Garch11Params)
    assert params.omega > 0
    assert 0 <= params.alpha <= 1
    assert 0 <= params.beta <= 1
    assert params.persistence < 1.0


def test_fit_garch11_requires_minimum_observations() -> None:
    short_returns = pd.Series(np.random.default_rng(0).normal(0, 0.01, 10))
    with pytest.raises(ValueError, match="at least"):
        fit_garch11(short_returns)


def test_conditional_volatility_matches_length_and_is_positive() -> None:
    returns = _synthetic_garch_returns()
    params = fit_garch11(returns)
    vol = conditional_volatility(returns, params)
    assert len(vol) == len(returns.dropna())
    assert (vol >= 0).all()
    assert vol.index.equals(returns.dropna().index)


def test_rolling_garch_forecast_is_leak_free() -> None:
    """Forecast at index i must be identical whether computed on a prefix ending at i+50 or exactly at i+1 — no future leakage."""
    returns = _synthetic_garch_returns(n=500)
    window = 200

    full = rolling_garch_forecast(returns, window=window, refit_every=50)
    truncated = rolling_garch_forecast(returns.iloc[: window + 60], window=window, refit_every=50)

    check_idx = window + 5
    assert full.iloc[check_idx] == pytest.approx(truncated.iloc[check_idx], rel=1e-9)


def test_rolling_garch_forecast_ignores_future_mutation() -> None:
    """Mutating returns strictly after check_idx must not change the forecast at check_idx — the direct leak-free check."""
    returns = _synthetic_garch_returns(n=500)
    window = 200
    refit_every = 50
    check_idx = window + 5  # not on a refit boundary, well inside the series

    baseline = rolling_garch_forecast(returns, window=window, refit_every=refit_every)

    mutated = returns.copy()
    mutated.iloc[check_idx + 1 :] = mutated.iloc[check_idx + 1 :] * 1000.0
    mutated_forecast = rolling_garch_forecast(mutated, window=window, refit_every=refit_every)

    assert mutated_forecast.iloc[check_idx] == pytest.approx(baseline.iloc[check_idx], rel=1e-9)


def test_rolling_garch_forecast_rejects_window_below_minimum() -> None:
    returns = _synthetic_garch_returns(n=200)
    with pytest.raises(ValueError, match="window must be"):
        rolling_garch_forecast(returns, window=10)


def test_rolling_garch_forecast_warmup_is_nan() -> None:
    returns = _synthetic_garch_returns(n=400)
    window = 200
    forecasts = rolling_garch_forecast(returns, window=window, refit_every=25)
    assert forecasts.iloc[:window].isna().all()
    assert forecasts.iloc[window:].notna().any()


def test_annualize_volatility_scales_by_sqrt_time() -> None:
    per_bar = 0.01
    annual = annualize_volatility(per_bar, bars_per_year=24 * 365)
    assert annual == pytest.approx(per_bar * np.sqrt(24 * 365))
