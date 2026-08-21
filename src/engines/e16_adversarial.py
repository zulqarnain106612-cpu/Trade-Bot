"""
E-16 — Adversarial Detection engine.

Spoofing detection on live orderbook events.
Wash trading: Benford's law on trade sizes.
Gap G-03 fix: penalty extends to E-17 too (not just E-02).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import structlog

from src.engines.schema import EngineOutput


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-16"
_SLA_SECONDS = 5
_CANCEL_WINDOW_MS = 500
_LARGE_WALL_SIGMA = 3.0
_BENFORD_THRESHOLD = 0.15  # max acceptable L1 deviation from Benford distribution

_BENFORD_EXPECTED = np.array([np.log10(1 + 1 / d) for d in range(1, 10)])


def spoof_confidence(orderbook_events: list[dict[str, Any]]) -> float:
    """
    Detect spoofing: large walls posted and cancelled within 500ms.

    Returns cancel_rate (0-1): fraction of large walls that were cancelled quickly.
    """
    if not orderbook_events:
        return 0.0
    sizes = np.array([e.get("size", 0.0) for e in orderbook_events])
    if len(sizes) == 0 or sizes.std() == 0:
        return 0.0
    size_threshold = sizes.mean() + _LARGE_WALL_SIGMA * sizes.std()
    large_walls = [e for e in orderbook_events if e.get("size", 0.0) > size_threshold]
    if not large_walls:
        return 0.0
    fast_cancels = sum(1 for w in large_walls if w.get("cancelled_ms", 99999) < _CANCEL_WINDOW_MS)
    return float(fast_cancels / len(large_walls))


def benford_deviation(trade_sizes: np.ndarray) -> float:
    """
    Benford's law deviation on trade sizes.

    Returns L1 distance from Benford expected distribution.
    Higher = more suspicious (0 = perfectly Benford-distributed).
    """
    valid = trade_sizes[trade_sizes > 0]
    if len(valid) < 30:
        return 0.0
    first_digits = np.array([int(str(float(s)).replace(".", "").lstrip("0")[0]) for s in valid])
    observed = np.bincount(first_digits, minlength=10)[1:] / len(first_digits)
    return float(np.sum(np.abs(observed - _BENFORD_EXPECTED)))


class E16Adversarial:
    def __init__(self, horizon_hours: int = 1) -> None:
        self._horizon = horizon_hours

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        spot: float = data.get("spot", 0.0)
        if spot <= 0:
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, "no_spot")

        try:
            ob_events: list[dict] = data.get("orderbook_events", [])
            trade_sizes: np.ndarray = np.array(data.get("trade_sizes", []))

            spoof_conf = spoof_confidence(ob_events)
            benford_dev = benford_deviation(trade_sizes) if len(trade_sizes) > 0 else 0.0
            volume_trust = max(0.0, 1.0 - benford_dev / _BENFORD_THRESHOLD)
            manipulation_flag = spoof_conf > 0.5 or benford_dev > _BENFORD_THRESHOLD

            if manipulation_flag:
                log.warning(
                    "manipulation_detected",
                    symbol=symbol,
                    spoof_conf=spoof_conf,
                    benford_dev=benford_dev,
                )

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=spot,
                confidence=float(1.0 - spoof_conf),
                direction=0,  # adversarial detection gives no directional signal
                horizon_hours=self._horizon,
                metadata={
                    "spoof_confidence": spoof_conf,
                    "volume_trust_score": volume_trust,
                    "benford_deviation": benford_dev,
                    "manipulation_flag": manipulation_flag,
                },
            )
        except Exception as exc:
            log.warning("e16_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))
