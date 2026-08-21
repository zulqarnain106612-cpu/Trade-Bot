"""
E-07 — PCA / Cointegration engine.

PCA: first principal component direction as trend signal.
Cointegration (VECM spread): z-score mean-reversion signal.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import structlog

from src.engines.schema import EngineOutput


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-07"
_SLA_SECONDS = 2
_ROLL_DAYS = 60 * 24  # 60 days hourly
_Z_THRESHOLD = 2.0


class E07LinearAlgebra:
    def __init__(self, horizon_hours: int = 4) -> None:
        self._horizon = horizon_hours

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        df: pd.DataFrame | None = data.get("ohlcv")
        spot: float = data.get("spot", 0.0)
        if df is None or len(df) < 60 or spot <= 0:
            return EngineOutput.abstain(
                _ENGINE_ID, symbol, spot, self._horizon, "insufficient_data"
            )

        try:
            correlated: dict[str, pd.DataFrame] = data.get("correlated_ohlcv", {})
            pca_dir = self._pca_signal(df, correlated)
            spread_z, coint_dir = self._cointegration_signal(df, correlated, spot)

            # Blend PCA and cointegration
            if abs(spread_z) > _Z_THRESHOLD:
                direction = coint_dir
                confidence = float(min(abs(spread_z) / 4.0, 1.0))
            else:
                direction = pca_dir
                confidence = 0.4  # lower confidence for PCA trend signal

            predicted = spot * (1 + direction * 0.002 * self._horizon)

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=predicted,
                confidence=confidence,
                direction=direction,
                horizon_hours=self._horizon,
                metadata={"spread_z": spread_z, "pca_direction": pca_dir},
            )
        except Exception as exc:
            log.warning("e07_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))

    @staticmethod
    def _pca_signal(df: pd.DataFrame, correlated: dict[str, pd.DataFrame]) -> int:
        all_returns = [df["close"].pct_change().dropna().values[-_ROLL_DAYS:]]
        all_returns.extend(
            cdf["close"].pct_change().dropna().values[-_ROLL_DAYS:]
            for cdf in correlated.values()
            if len(cdf) >= 60
        )

        min_len = min(len(r) for r in all_returns)
        if min_len < 20:
            return 0

        matrix = np.column_stack([r[-min_len:] for r in all_returns])
        matrix -= matrix.mean(axis=0)
        _, _, Vt = np.linalg.svd(matrix, full_matrices=False)
        pc1_direction = Vt[0, 0]  # first coin's loading on PC1
        return 1 if pc1_direction > 0 else -1

    @staticmethod
    def _cointegration_signal(
        df: pd.DataFrame, correlated: dict[str, pd.DataFrame], spot: float
    ) -> tuple[float, int]:
        if not correlated:
            return 0.0, 0
        # Use first correlated asset for simple spread
        peer_df = next(iter(correlated.values()))
        n = min(len(df), len(peer_df), _ROLL_DAYS)
        if n < 30:
            return 0.0, 0

        prices = df["close"].values[-n:]
        peer = peer_df["close"].values[-n:]

        # OLS beta for spread
        beta = float(np.cov(prices, peer)[0, 1] / (np.var(peer) + 1e-12))
        spread = prices - beta * peer
        mu = spread.mean()
        sigma = spread.std()
        z = (spread[-1] - mu) / (sigma + 1e-12)

        direction = -1 if z > _Z_THRESHOLD else (1 if z < -_Z_THRESHOLD else 0)
        return float(z), direction
