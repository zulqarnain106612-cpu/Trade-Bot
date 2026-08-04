"""Tests for EngineOutput schema contract."""

from datetime import UTC, datetime

from src.engines.schema import EngineOutput


def _make(direction: int = 1, confidence: float = 0.8) -> EngineOutput:
    return EngineOutput(
        engine_id="E-01",
        symbol="BTC/USDT",
        timestamp_utc=datetime.now(UTC),
        predicted_price=50_000.0,
        confidence=confidence,
        direction=direction,
        horizon_hours=4,
    )


def test_confidence_clamped():
    o = _make(confidence=1.5)
    assert o.confidence == 1.0

    o2 = _make(confidence=-0.5)
    assert o2.confidence == 0.0


def test_direction_sanitized():
    o = _make(direction=99)
    assert o.direction == 0


def test_valid_directions():
    for d in (-1, 0, 1):
        o = _make(direction=d)
        assert o.direction == d


def test_abstain_factory():
    o = EngineOutput.abstain("E-05", "LTC/USDT", 90.0, horizon_hours=4, reason="no_data")
    assert o.confidence == 0.0
    assert o.direction == 0
    assert o.predicted_price == 90.0
    assert o.metadata["abstain_reason"] == "no_data"
