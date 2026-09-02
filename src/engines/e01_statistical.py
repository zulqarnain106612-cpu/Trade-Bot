"""
E-01 — Statistical / HMM / ARIMA engine.

Wraps the existing HMM regime detector and adds ARIMA(auto) on a rolling
180-candle window. GRU deferred to E-09 (meta-learner).
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import structlog

from src.engines.schema import EngineOutput

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-01"
_SLA_SECONDS = 2


class E01Statistical:
    """ARIMA + HMM statistical engine."""

    def __init__(self, horizon_hours: int = 4) -> None:
        self._horizon = horizon_hours

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        import asyncio

        df: pd.DataFrame | None = data.get("ohlcv")
        spot: float = data.get("spot", 0.0)
        if df is None or len(df) < 30 or spot <= 0:
            return EngineOutput.abstain(
                _ENGINE_ID, symbol, spot, self._horizon, "insufficient_data"
            )

        try:
            loop = asyncio.get_running_loop()
            arima_pred = await loop.run_in_executor(None, self._arima_predict, df, spot)
            hmm_conf = self._hmm_confidence(data)
            confidence = max(0.0, min(1.0, hmm_conf))
            direction = 1 if arima_pred > spot * 1.001 else (-1 if arima_pred < spot * 0.999 else 0)
            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=arima_pred,
                confidence=confidence,
                direction=direction,
                horizon_hours=self._horizon,
                metadata={"arima_pred": arima_pred, "hmm_conf": hmm_conf},
            )
        except Exception as exc:
            log.warning("e01_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))

    # ------------------------------------------------------------------
    # ARIMA via pmdarima (auto_arima)
    # ------------------------------------------------------------------

    @staticmethod
    def _arima_predict(df: pd.DataFrame, spot: float) -> float:
        try:
            from pmdarima import auto_arima  # type: ignore[import]

            series = df["close"].values[-180:]
            model = auto_arima(
                series,
                start_p=1,
                max_p=3,
                start_q=0,
                max_q=2,
                d=None,
                seasonal=False,
                error_action="ignore",
                suppress_warnings=True,
                stepwise=True,
            )
            # LAW9: take the 95% confidence interval alongside the point forecast
            # and reject the fit when the interval is degenerate or wider than the
            # spot price — such a forecast carries no usable information.
            forecast, conf_int = model.predict(n_periods=1, return_conf_int=True)  # confidence
            low, high = float(conf_int[0][0]), float(conf_int[0][1])
            point = float(forecast[0])
            if not np.isfinite([low, high, point]).all() or (high - low) > abs(spot):
                raise ValueError("arima confidence interval too wide")
            return point
        except Exception:
            # Fallback: naive drift
            window = df["close"].values[-20:]
            drift = (window[-1] - window[0]) / len(window)
            return float(spot + drift)

    # ------------------------------------------------------------------
    # HMM confidence from existing detector
    # ------------------------------------------------------------------

    @staticmethod
    def _hmm_confidence(data: dict) -> float:
        regime_probs = data.get("regime_probs")
        if regime_probs is not None and len(regime_probs) > 0:
            return float(np.max(regime_probs))
        return 0.5
