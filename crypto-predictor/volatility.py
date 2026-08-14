"""
GARCH(1,1) volatility modeling — Bollerslev (1986).

Crypto returns show strong volatility clustering (large moves follow large
moves, regardless of direction). A GARCH model captures this explicitly,
unlike a plain rolling standard deviation which weights all lookback points
equally and reacts slowly to regime shifts.

GARCH(1,1):
    sigma_t^2 = omega + alpha * epsilon_{t-1}^2 + beta * sigma_{t-1}^2

where epsilon_{t-1} is the previous period's return shock. This produces a
forward-looking conditional volatility estimate, used here as:
  1. A feature for the ML ensemble (current volatility regime)
  2. An input to ATR-independent stop-loss/position-sizing logic
"""

import numpy as np
import pandas as pd
from arch import arch_model


def fit_garch_volatility(returns: pd.Series, scale: float = 100.0) -> pd.Series:
    """
    Fits GARCH(1,1) on a return series and returns the in-sample conditional
    volatility (annualization NOT applied here — raw per-period sigma).

    returns: pct-change series (e.g. df['close'].pct_change().dropna())
    scale:   arch_model is numerically unstable on tiny return magnitudes
             (crypto hourly returns ~0.001), so returns are scaled up for
             fitting and the output is scaled back down to match.
    """
    clean_returns = returns.dropna() * scale
    if len(clean_returns) < 50:
        raise ValueError("Need at least 50 return observations to fit GARCH reliably")

    model = arch_model(clean_returns, vol="Garch", p=1, q=1, dist="normal", rescale=False)
    fitted = model.fit(disp="off")

    conditional_vol = fitted.conditional_volatility / scale
    conditional_vol.index = clean_returns.index
    return conditional_vol


def rolling_garch_forecast(
    returns: pd.Series, window: int = 300, scale: float = 100.0
) -> pd.Series:
    """
    Walk-forward GARCH: refits on a trailing window and forecasts ONE step
    ahead only, so the forecast at time t never uses information from t or
    later. This is the leak-free version suitable for feeding a backtester
    (the plain fit_garch_volatility above is in-sample and must not be used
    directly as a backtest feature).
    """
    clean = returns.dropna() * scale
    forecasts = pd.Series(index=clean.index, dtype=float)

    for i in range(window, len(clean)):
        train_slice = clean.iloc[i - window : i]
        try:
            model = arch_model(train_slice, vol="Garch", p=1, q=1, dist="normal", rescale=False)
            fitted = model.fit(disp="off", show_warning=False)
            f = fitted.forecast(horizon=1, reindex=False)
            next_vol = np.sqrt(f.variance.values[-1, 0]) / scale
        except Exception:
            next_vol = np.nan
        forecasts.iloc[i] = next_vol

    return forecasts


if __name__ == "__main__":
    from data_fetch_kraken import fetch_ohlcv

    df = fetch_ohlcv("XBTUSD", "1h")
    rets = df["close"].pct_change()

    print("Fitting in-sample GARCH(1,1) for a sanity check...")
    vol = fit_garch_volatility(rets)
    print(
        f"Latest conditional volatility (1h): {vol.iloc[-1]:.5f} "
        f"({vol.iloc[-1]*100:.3f}% per hour)"
    )
    print(f"Annualized (approx): {vol.iloc[-1] * np.sqrt(24*365) * 100:.2f}%")
