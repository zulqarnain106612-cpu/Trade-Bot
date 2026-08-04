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
