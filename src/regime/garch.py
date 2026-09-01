"""
GARCH(1,1) conditional volatility — Bollerslev (1986).

Crypto returns show strong volatility clustering (large moves cluster in
time regardless of direction). The HMM regime detector (src/regime/
detector.py) captures this indirectly via `realized_vol_ratio`, but that is
a backward-looking rolling statistic — it weights all points in the window
equally and reacts slowly to a fresh volatility shock. GARCH(1,1) is the
standard forward-looking alternative:

    sigma_t^2 = omega + alpha * epsilon_{t-1}^2 + beta * sigma_{t-1}^2

Fit here via direct Gaussian quasi-MLE (scipy.optimize), not the third-party
`arch` package, to avoid adding a new pinned dependency for one model.

Two entry points:
  - `_fit_garch11_offline`: in-sample fit — diagnostics / research only, must NOT
    be used as a live or backtest feature (uses the full series to estimate
    the params that generate its own conditional variance). Underscore-prefixed
    to prevent accidental import into the live pipeline.
  - `rolling_garch_forecast`: leak-free walk-forward — refits on a trailing
    window and forecasts exactly one step ahead, so the forecast at index i
    uses only returns strictly before i. Safe for backtest/feature use.

Authority:
  Bollerslev, T. (1986) "Generalized Autoregressive Conditional
    Heteroskedasticity", Journal of Econometrics 31(3).
  López de Prado (2018) AFML — no-look-ahead requirement for time-series
    features (Ch.5 caution on fitting-then-serving the same window).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import structlog
from scipy.optimize import minimize

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Returns are scaled up before fitting: raw crypto per-bar returns (~1e-3)
# make the GARCH log-likelihood surface numerically flat, which stalls
# L-BFGS-B. Scaling back down after fitting recovers the true-units sigma.
_SCALE: float = 100.0
_MIN_OBS: int = 50


@dataclass(frozen=True, slots=True)
class Garch11Params:
    """Fitted GARCH(1,1) coefficients, in scaled-return units."""

    omega: float
    alpha: float
    beta: float
    scale: float

    @property
    def persistence(self) -> float:
        """alpha + beta: how slowly volatility shocks decay (must be < 1 for stationarity)."""
        return self.alpha + self.beta

    @property
    def unconditional_variance(self) -> float:
        """Long-run variance the process reverts to, in scaled units."""
        denom = 1.0 - self.persistence
        if denom <= 1e-8:
            return float("nan")
        return self.omega / denom


def _neg_log_likelihood(theta: np.ndarray, scaled_returns: np.ndarray) -> float:
    omega, alpha, beta = theta
    if omega <= 0 or alpha < 0 or beta < 0 or alpha + beta >= 1.0:
        return 1e10

    n = len(scaled_returns)
    sigma2 = np.empty(n)
    sigma2[0] = float(np.var(scaled_returns))
    for t in range(1, n):
        sigma2[t] = omega + alpha * scaled_returns[t - 1] ** 2 + beta * sigma2[t - 1]

    sigma2 = np.maximum(sigma2, 1e-12)
    ll = -0.5 * np.sum(np.log(2 * np.pi * sigma2) + (scaled_returns**2) / sigma2)
    return -float(ll)


def _fit_garch11_offline(returns: pd.Series, scale: float = _SCALE) -> Garch11Params:
    """
    In-sample GARCH(1,1) fit via Gaussian quasi-MLE.

    NOT leak-free — the fitted params depend on the entire input series.
    Use ONLY for offline diagnostics/research. For any feature, backtest,
    or live signal use `rolling_garch_forecast` instead — it re-fits on a
    trailing window and forecasts exactly one step ahead, so index i uses
    only returns before i.

    Prefixed with `_` to prevent accidental import into the live feature
    pipeline. Any caller outside tests/scripts is a bug.
    """
    clean = returns.dropna().to_numpy() * scale
    if len(clean) < _MIN_OBS:
        raise ValueError(f"Need at least {_MIN_OBS} return observations to fit GARCH(1,1)")

    var0 = float(np.var(clean))
    x0 = np.array([var0 * 0.05, 0.05, 0.90])
    bounds = [(1e-10, None), (0.0, 1.0), (0.0, 1.0)]

    result = minimize(
        _neg_log_likelihood,
        x0,
        args=(clean,),
        method="L-BFGS-B",
        bounds=bounds,
    )
    omega, alpha, beta = result.x
    return Garch11Params(omega=float(omega), alpha=float(alpha), beta=float(beta), scale=scale)


def conditional_volatility(returns: pd.Series, params: Garch11Params) -> pd.Series:
    """In-sample conditional volatility path implied by `params`, in raw-return units."""
    clean = returns.dropna()
    scaled = clean.to_numpy() * params.scale
    n = len(scaled)
    sigma2 = np.empty(n)
    # Seeded with the fitted long-run (unconditional) variance rather than
    # the sample variance of `scaled` — consistent with treating `params`
    # as already-converged, so the recursion starts at its own steady
    # state instead of re-deriving a seed from the data it was fit on.
    # (_neg_log_likelihood seeds sigma2[0] with sample variance instead,
    # since during optimization there is no fitted long-run variance yet.)
    sigma2[0] = (
        params.unconditional_variance
        if np.isfinite(params.unconditional_variance)
        else float(np.var(scaled))
    )
    for t in range(1, n):
        sigma2[t] = params.omega + params.alpha * scaled[t - 1] ** 2 + params.beta * sigma2[t - 1]
    sigma = np.sqrt(np.maximum(sigma2, 0.0)) / params.scale
    return pd.Series(sigma, index=clean.index, name="garch_conditional_vol")


def rolling_garch_forecast(
    returns: pd.Series,
    window: int = 300,
    refit_every: int = 10,
    scale: float = _SCALE,
) -> pd.Series:
    """
    Leak-free walk-forward one-step-ahead GARCH(1,1) volatility forecast.

    For each index i >= window, refits on returns[i-window:i] (refitting
    every `refit_every` bars to amortize the O(window) MLE cost — the
    trailing window shifts by only one bar between forecasts so refitting
    every bar buys little accuracy for a real cost increase) and forecasts
    sigma_{i} using only information available strictly before i. Points
    before `window` are NaN (insufficient history) — callers must handle
    this the same way as other warm-up-period features in this pipeline
    (see realized_vol_ratio / atr_momentum in src/features/pipeline.py).
    """
    if window < _MIN_OBS:
        raise ValueError(
            f"window must be >= {_MIN_OBS} observations to fit GARCH(1,1) reliably, got {window}"
        )

    clean = returns.dropna()
    scaled = clean.to_numpy() * scale
    n = len(scaled)
    forecasts = pd.Series(index=clean.index, dtype=float, name="garch_vol_forecast")

    params: Garch11Params | None = None
    # Seeded only in case the very first refit (i == window) throws before
    # producing a fit; the except branch below falls back to sample
    # variance of the window rather than leaving last_sigma2 undefined.
    last_sigma2 = float(np.var(scaled[:window]))

    for i in range(window, n):
        train = scaled[i - window : i]
        if params is None or (i - window) % refit_every == 0:
            try:
                var0 = float(np.var(train))
                x0 = np.array([var0 * 0.05, 0.05, 0.90])
                result = minimize(
                    _neg_log_likelihood,
                    x0,
                    args=(train,),
                    method="L-BFGS-B",
                    bounds=[(1e-10, None), (0.0, 1.0), (0.0, 1.0)],
                )
                omega, alpha, beta = result.x
                params = Garch11Params(
                    omega=float(omega), alpha=float(alpha), beta=float(beta), scale=scale
                )
                sigma2_path = np.empty(len(train))
                sigma2_path[0] = var0
                for t in range(1, len(train)):
                    sigma2_path[t] = omega + alpha * train[t - 1] ** 2 + beta * sigma2_path[t - 1]
                last_sigma2 = float(sigma2_path[-1])
            except Exception as exc:
                log.warning(
                    "garch.rolling_forecast_refit_failed", index=i, error=str(exc), exc_info=True
                )
                params = None
                last_sigma2 = float(np.var(train))

        last_eps = train[-1]
        if params is not None:
            next_var = params.omega + params.alpha * last_eps**2 + params.beta * last_sigma2
            last_sigma2 = next_var
            forecasts.iloc[i] = np.sqrt(max(next_var, 0.0)) / scale
        else:
            forecasts.iloc[i] = np.nan

    return forecasts


def annualize_volatility(per_bar_vol: float, bars_per_year: float) -> float:
    """Scale a per-bar sigma to an annualized figure (e.g. bars_per_year=24*365 for hourly bars)."""
    return per_bar_vol * float(np.sqrt(bars_per_year))
