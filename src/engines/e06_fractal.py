"""
E-06 — Fractal / Hurst / Chaos engine.

DFA-method Hurst exponent on rolling 256 candles.
H > 0.6 → trending; H < 0.4 → mean-reverting; else neutral.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import structlog

from src.engines.schema import EngineOutput


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-06"
_SLA_SECONDS = 3
_WINDOW = 256


def hurst_dfa(series: np.ndarray, min_scale: int = 4, max_scale: int = 64) -> float:
    """
    Detrended Fluctuation Analysis Hurst exponent.

    H ≈ 0.5 → random walk, H > 0.5 → persistent, H < 0.5 → anti-persistent.
    """
    n = len(series)
    if n < min_scale * 4:
        return 0.5

    scales = np.unique(
        np.round(np.logspace(np.log10(min_scale), np.log10(min(max_scale, n // 4)), 10)).astype(int)
    )
    fluctuations = []
    valid_scales = []

    cumsum = np.cumsum(series - series.mean())

    for scale in scales:
        n_segments = n // scale
        if n_segments < 2:
            continue
        f_sq = []
        for seg in range(n_segments):
            segment = cumsum[seg * scale : (seg + 1) * scale]
            t = np.arange(scale)
            coeffs = np.polyfit(t, segment, 1)
            trend = np.polyval(coeffs, t)
            f_sq.append(np.mean((segment - trend) ** 2))
        fluctuations.append(np.sqrt(np.mean(f_sq)))
        valid_scales.append(scale)

    if len(valid_scales) < 2:
        return 0.5

    # Filter out zero fluctuations to avoid log(0) = -inf corrupting polyfit
    pairs = [(s, f) for s, f in zip(valid_scales, fluctuations, strict=True) if f > 0]
    if len(pairs) < 2:
        return 0.5
    log_scales = np.log([s for s, _ in pairs])
    log_fluct = np.log([f for _, f in pairs])
    slope, _ = np.polyfit(log_scales, log_fluct, 1)
    return float(np.clip(slope, 0.0, 1.0))


class E06Fractal:
    def __init__(self, horizon_hours: int = 4) -> None:
        self._horizon = horizon_hours

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        df: pd.DataFrame | None = data.get("ohlcv")
        spot: float = data.get("spot", 0.0)
        if df is None or len(df) < 64 or spot <= 0:
            return EngineOutput.abstain(
                _ENGINE_ID, symbol, spot, self._horizon, "insufficient_data"
            )

        try:
            returns = df["close"].pct_change().dropna().values[-_WINDOW:]
            h = hurst_dfa(returns)
            confidence = float(abs(h - 0.5) * 2)  # |H - 0.5| * 2

            last_drift = float(np.mean(returns[-10:]))
            direction = self._direction(h, last_drift)

            # Price forecast: H-weighted drift projection
            predicted = spot * (1 + last_drift * self._horizon * (2 * h - 1))
            predicted = float(np.clip(predicted, spot * 0.8, spot * 1.2))

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=predicted,
                confidence=confidence,
                direction=direction,
                horizon_hours=self._horizon,
                metadata={"hurst": h},
            )
        except Exception as exc:
            log.warning("e06_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))

    @staticmethod
    def _direction(h: float, last_drift: float) -> int:
        if h > 0.6:
            return 1 if last_drift > 0 else -1
        if h < 0.4:
            return -1 if last_drift > 0 else 1  # mean-revert
        return 0
