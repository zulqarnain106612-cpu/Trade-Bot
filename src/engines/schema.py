"""
Shared output schema for all 18 Crypto-Box engines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class EngineOutput:
    engine_id: str  # "E-01" through "E-18"
    symbol: str  # "BTC/USDT"
    timestamp_utc: datetime
    predicted_price: float  # consensus target
    confidence: float  # 0.0-1.0
    direction: int  # +1 long, -1 short, 0 neutral
    horizon_hours: int  # 1, 4, or 24
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.confidence = max(0.0, min(1.0, self.confidence))
        if self.direction not in (-1, 0, 1):
            self.direction = 0

    @classmethod
    def abstain(
        cls,
        engine_id: str,
        symbol: str,
        spot: float,
        horizon_hours: int = 4,
        reason: str = "",
    ) -> EngineOutput:
        """Return a zero-confidence, neutral output when an engine cannot produce a signal."""
        return cls(
            engine_id=engine_id,
            symbol=symbol,
            timestamp_utc=datetime.now(UTC),
            predicted_price=spot,
            confidence=0.0,
            direction=0,
            horizon_hours=horizon_hours,
            metadata={"abstain_reason": reason},
        )
