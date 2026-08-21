"""
E-04 — Fourier / Cyclical / Halving Patterns engine.

FFT on log-price series to find dominant cycle periods.
BTC-only: halving cycle overlay.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import structlog

from src.engines.schema import EngineOutput


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-04"
_SLA_SECONDS = 5
_WINDOW = 365 * 24  # 365 days of hourly data


class E04Fourier:
    def __init__(self, horizon_hours: int = 24) -> None:
        self._horizon = horizon_hours

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        df: pd.DataFrame | None = data.get("ohlcv")
        spot: float = data.get("spot", 0.0)
        if df is None or len(df) < 128 or spot <= 0:
            return EngineOutput.abstain(
                _ENGINE_ID, symbol, spot, self._horizon, "insufficient_data"
            )

        try:
            series = np.log(df["close"].values[-_WINDOW:])
            predicted, explained_var = self._fft_predict(series, spot, self._horizon)

            direction = 1 if predicted > spot * 1.002 else (-1 if predicted < spot * 0.998 else 0)
            confidence = float(np.clip(explained_var, 0.0, 1.0))

            coin = symbol.split("/")[0].upper()
            if coin == "BTC":
                halving_adj = self._halving_overlay(data.get("block_height", 0))
                predicted *= 1 + halving_adj * 0.001

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=predicted,
                confidence=confidence,
                direction=direction,
                horizon_hours=self._horizon,
                metadata={"explained_variance": explained_var},
            )
        except Exception as exc:
            log.warning("e04_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))

    @staticmethod
    def _fft_predict(log_prices: np.ndarray, spot: float, horizon: int) -> tuple[float, float]:
        n = len(log_prices)
        fft = np.fft.rfft(log_prices - log_prices.mean())
        magnitudes = np.abs(fft)
        # Top-3 dominant frequencies
        top3_idx = np.argsort(magnitudes)[-3:]
        freqs = np.fft.rfftfreq(n)

        # Reconstruct top-3 and project forward
        reconstruction = np.zeros(n + horizon)
        total_power = (magnitudes**2).sum()
        captured_power = 0.0
        for idx in top3_idx:
            amp = magnitudes[idx] * 2 / n
            freq = freqs[idx]
            phase = np.angle(fft[idx])
            t = np.arange(n + horizon)
            reconstruction += amp * np.cos(2 * np.pi * freq * t + phase)
            captured_power += magnitudes[idx] ** 2

        explained_var = float(captured_power / (total_power + 1e-12))
        baseline = log_prices.mean()
        predicted_log = baseline + reconstruction[n + horizon - 1]
        predicted = float(np.exp(predicted_log))

        # Sanity cap: max ±30% move
        predicted = float(np.clip(predicted, spot * 0.7, spot * 1.3))
        return predicted, explained_var

    @staticmethod
    def _halving_overlay(block_height: int) -> float:
        """Cycle position as fraction of 4-year halving cycle."""
        blocks_per_halving = 210_000
        cycle_pos = (block_height % blocks_per_halving) / blocks_per_halving
        # Post-halving: first 50% of cycle historically bullish
        return 1.0 if cycle_pos < 0.5 else -0.5
