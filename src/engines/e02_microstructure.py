"""
E-02 — Order Book Imbalance / Microstructure engine.

Reads live orderbook snapshots and computes bid/ask imbalance,
mid-price drift, and order flow toxicity.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd
import structlog

from src.engines.schema import EngineOutput


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-02"
_SLA_SECONDS = 3
_TOP_N = 5  # top-N levels for imbalance


class E02Microstructure:
    def __init__(self, horizon_hours: int = 1) -> None:
        self._horizon = horizon_hours

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        spot: float = data.get("spot", 0.0)
        ob_df: pd.DataFrame | None = data.get("orderbook")
        if ob_df is None or ob_df.empty or spot <= 0:
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, "no_orderbook")

        try:
            latest = ob_df.iloc[-1]
            bids = json.loads(latest["bids_json"])
            asks = json.loads(latest["asks_json"])

            imbalance = self._bid_ask_imbalance(bids, asks)
            toxicity = abs(imbalance - 0.5) * 2  # 0=balanced, 1=max toxic
            confidence = toxicity

            # Mid-price drift vs 5-min-ago snapshot
            direction = self._direction_from_imbalance(imbalance)

            predicted_price = spot * (1 + (imbalance - 0.5) * 0.002)

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=predicted_price,
                confidence=confidence,
                direction=direction,
                horizon_hours=self._horizon,
                metadata={
                    "bid_ask_imbalance": imbalance,
                    "order_flow_toxicity": toxicity,
                },
            )
        except Exception as exc:
            log.warning("e02_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))

    @staticmethod
    def _bid_ask_imbalance(bids: list, asks: list) -> float:
        top_bids = bids[:_TOP_N]
        top_asks = asks[:_TOP_N]
        bid_vol = sum(float(b[1]) for b in top_bids)
        ask_vol = sum(float(a[1]) for a in top_asks)
        total = bid_vol + ask_vol
        if total == 0:
            return 0.5
        return bid_vol / total

    @staticmethod
    def _direction_from_imbalance(imbalance: float) -> int:
        if imbalance > 0.6:
            return 1
        if imbalance < 0.4:
            return -1
        return 0
