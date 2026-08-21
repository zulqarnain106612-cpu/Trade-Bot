"""
E-14 — Sentiment Quantification engine.

Combines Fear & Greed index + VADER NLP headlines.
Contrarian signal: extreme fear → +1, extreme greed → -1.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

import structlog

from src.engines.schema import EngineOutput


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-14"
_SLA_SECONDS = 5

# Calibrated weights (FG, NLP, social volume)
_ALPHA = 0.5
_BETA = 0.3
_GAMMA = 0.2

# Historical FG calibration: FG < 20 → +40% 30d; FG > 80 → -15% 30d
_BULLISH_FG_THRESHOLD = 20.0
_BEARISH_FG_THRESHOLD = 80.0


def raw_sentiment(fg: float, nlp: float, social_vol: float = 0.0) -> float:
    return _ALPHA * (fg / 100.0) + _BETA * ((nlp + 1) / 2.0) + _GAMMA * social_vol


def contrarian_signal(raw: float, window_min: float = 0.0, window_max: float = 1.0) -> float:
    """Normalized contrarian signal: 1.0 = buy (extreme fear), -1.0 = sell (extreme greed)."""
    normalized = (raw - window_min) / max(window_max - window_min, 1e-9)
    return float(1.0 - 2.0 * normalized)


class E14Sentiment:
    def __init__(self, horizon_hours: int = 24) -> None:
        self._horizon = horizon_hours
        self._raw_history: deque[float] = deque(maxlen=1000)

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        spot: float = data.get("spot", 0.0)
        sentiment = data.get("sentiment", {})
        fg_score: float = float(sentiment.get("fg_score", 50.0))
        vader_compound: float = float(sentiment.get("vader_compound", 0.0))
        social_vol: float = float(sentiment.get("social_vol", 0.0))

        if spot <= 0:
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, "no_spot")

        try:
            raw = raw_sentiment(fg_score, vader_compound, social_vol)
            self._raw_history.append(raw)

            win_min = min(self._raw_history) if self._raw_history else 0.0
            win_max = max(self._raw_history) if self._raw_history else 1.0

            signal = contrarian_signal(raw, win_min, win_max)
            direction = 1 if signal > 0.3 else (-1 if signal < -0.3 else 0)
            confidence = float(abs(signal))

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=spot * (1 + direction * 0.002),
                confidence=min(confidence, 1.0),
                direction=direction,
                horizon_hours=self._horizon,
                metadata={
                    "fg_score": fg_score,
                    "vader_compound": vader_compound,
                    "raw_sentiment": raw,
                    "contrarian_signal": signal,
                },
            )
        except Exception as exc:
            log.warning("e14_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))
