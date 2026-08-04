"""
Integration test for engine orchestrator.

Verifies graceful degradation when engines time-out or error.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.engines.orchestrator import EngineOrchestrator
from src.engines.schema import EngineOutput


def make_ohlcv(n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    closes = np.cumprod(1 + rng.normal(0, 0.01, n)) * 50_000.0
    opens = closes * (1 + rng.normal(0, 0.002, n))
    highs = np.maximum(opens, closes) * 1.005
    lows = np.minimum(opens, closes) * 0.995
    vols = rng.uniform(1000, 5000, n)
    ts = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp_utc": ts,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vols,
        }
    )


@pytest.mark.asyncio
async def test_orchestrator_returns_result():
    orch = EngineOrchestrator()
    data = {
        "ohlcv": make_ohlcv(),
        "spot": 50_000.0,
        "regime": "Trending",
        "sentiment": {"fg_score": 50.0, "vader_compound": 0.0},
        "macro": None,
        "orderbook": None,
        "options": None,
        "exchange_flows": [],
    }
    result = await orch.run("BTC/USDT", data)
    assert result.symbol == "BTC/USDT"
    assert result.trade_signal.direction in (-1, 0, 1)
    assert 0 <= result.trade_signal.kelly_multiplier <= 1
    assert result.consensus.consensus_price > 0


@pytest.mark.asyncio
async def test_orchestrator_graceful_degradation():
    """Engines that throw exceptions are removed from consensus — no crash."""
    orch = EngineOrchestrator()

    # Patch E-01 to raise
    async def broken(*a, **kw) -> EngineOutput:
        raise RuntimeError("engine_broken")

    orch._engines[0].run = broken

    data = {
        "ohlcv": make_ohlcv(),
        "spot": 50_000.0,
        "regime": "Ranging",
        "sentiment": {"fg_score": 30.0, "vader_compound": -0.1},
        "macro": None,
        "orderbook": None,
        "options": None,
        "exchange_flows": [],
    }
    result = await orch.run("BTC/USDT", data)
    assert "E-01" in result.failed_engines
    assert result.trade_signal.direction in (-1, 0, 1)


@pytest.mark.asyncio
async def test_orchestrator_all_engines_fail():
    """If all engines fail, circuit breaker / suppression returns neutral signal."""
    orch = EngineOrchestrator()

    async def broken(*a, **kw) -> EngineOutput:
        raise RuntimeError("all_broken")

    for engine in orch._engines:
        engine.run = broken

    data = {"ohlcv": make_ohlcv(), "spot": 50_000.0, "regime": "Volatile"}
    result = await orch.run("BTC/USDT", data)
    assert result.trade_signal.direction == 0
    assert result.trade_signal.kelly_multiplier == 0.0


@pytest.mark.asyncio
async def test_orchestrator_e16_circuit_breaker_suppresses():
    """E-16 manipulation_flag=True triggers circuit breaker: direction=0."""
    from datetime import UTC, datetime

    orch = EngineOrchestrator()

    # Patch E-16 (index 15) to return manipulation_flag=True
    async def manipulated(*a, **kw) -> EngineOutput:
        return EngineOutput(
            engine_id="E-16",
            symbol="BTC/USDT",
            timestamp_utc=datetime.now(UTC),
            predicted_price=50000.0,
            confidence=0.8,
            direction=0,
            horizon_hours=4,
            metadata={"manipulation_flag": True},
        )

    orch._engines[15].run = manipulated

    data = {
        "ohlcv": make_ohlcv(),
        "spot": 50_000.0,
        "regime": "Trending",
        "orderbook_events": [],
        "trade_sizes": [],
    }
    result = await orch.run("BTC/USDT", data)
    # Circuit breaker should suppress direction
    assert result.trade_signal.direction == 0


@pytest.mark.asyncio
async def test_orchestrator_audit_log_written(tmp_path):
    """engine_outputs parquet audit log is written to data_root."""
    orch = EngineOrchestrator(data_root=tmp_path)
    data = {
        "ohlcv": make_ohlcv(200),
        "spot": 50_000.0,
        "regime": "Ranging",
    }
    result = await orch.run("BTC/USDT", data)
    assert len(result.engine_outputs) > 0
    audit_files = list((tmp_path / "engine_outputs").glob("*.parquet"))
    assert len(audit_files) == 1
    import pandas as pd

    df = pd.read_parquet(audit_files[0])
    assert "engine_id" in df.columns
    assert len(df) == len(result.engine_outputs)
