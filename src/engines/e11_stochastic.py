"""
E-11 — Stochastic Calculus engine (GBM / Heston / Merton).

Yang-Zhang volatility estimator, GBM Monte Carlo, Merton jump-diffusion.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import structlog

from src.engines.schema import EngineOutput


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-11"
_SLA_SECONDS = 8
_MC_PATHS = 2000


def yang_zhang_vol(df: pd.DataFrame, window: int = 21) -> float:
    """Yang-Zhang (2000) close-to-close + RS estimator — annualized."""
    if len(df) < window + 1:
        return float(df["close"].pct_change().std() * np.sqrt(8760))

    log_oc = np.log(df["open"] / df["close"].shift(1)).dropna()
    log_co = np.log(df["close"] / df["open"]).dropna()
    log_ho = np.log(df["high"] / df["open"]).dropna()
    log_lo = np.log(df["low"] / df["open"]).dropna()

    n = min(len(log_oc), len(log_co), len(log_ho), len(log_lo), window)
    log_oc, log_co, log_ho, log_lo = (
        log_oc.values[-n:],
        log_co.values[-n:],
        log_ho.values[-n:],
        log_lo.values[-n:],
    )

    sigma_oc = np.var(log_oc)
    sigma_co = np.var(log_co)
    rs = np.mean(log_ho * (log_ho - log_co) + log_lo * (log_lo - log_co))
    k = 0.34 / (1.34 + (window + 1) / (window - 1))
    var = sigma_oc + k * sigma_co + (1 - k) * rs
    return float(np.sqrt(max(var, 0.0)) * np.sqrt(8760))


def gbm_mc(s0: float, mu: float, sigma: float, t_hours: float, n: int = _MC_PATHS) -> np.ndarray:
    """GBM Monte Carlo: returns terminal price distribution."""
    dt = t_hours / 8760
    z = np.random.standard_normal(n)
    return s0 * np.exp((mu - 0.5 * sigma**2) * dt + sigma * np.sqrt(dt) * z)


def merton_jump_prob(returns: np.ndarray, sigma: float, threshold_sigma: float = 3.0) -> float:
    """Estimate jump intensity from fat-tail event frequency."""
    threshold = threshold_sigma * sigma / np.sqrt(8760)
    jumps = np.sum(np.abs(returns) > threshold)
    return float(jumps / len(returns)) if len(returns) > 0 else 0.0


class E11Stochastic:
    def __init__(self, horizon_hours: int = 4) -> None:
        self._horizon = horizon_hours

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        df: pd.DataFrame | None = data.get("ohlcv")
        spot: float = data.get("spot", 0.0)
        if df is None or len(df) < 22 or spot <= 0:
            return EngineOutput.abstain(
                _ENGINE_ID, symbol, spot, self._horizon, "insufficient_data"
            )

        try:
            yz_vol = yang_zhang_vol(df)
            returns = df["close"].pct_change().dropna().values
            mu = float(np.mean(returns) * 8760)

            # GBM simulation
            terminal = gbm_mc(spot, mu, yz_vol, float(self._horizon))
            expected = float(np.mean(terminal))
            pct5 = float(np.percentile(terminal, 5))
            pct95 = float(np.percentile(terminal, 95))

            jump_prob = merton_jump_prob(returns, yz_vol)

            direction = 1 if expected > spot * 1.002 else (-1 if expected < spot * 0.998 else 0)
            confidence = float(1.0 - min(jump_prob * 3, 0.8))  # jumps reduce confidence

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=expected,
                confidence=confidence,
                direction=direction,
                horizon_hours=self._horizon,
                metadata={
                    "yz_vol": yz_vol,
                    "mu_annualized": mu,
                    "pct5": pct5,
                    "pct95": pct95,
                    "jump_prob": jump_prob,
                },
            )
        except Exception as exc:
            log.warning("e11_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))
