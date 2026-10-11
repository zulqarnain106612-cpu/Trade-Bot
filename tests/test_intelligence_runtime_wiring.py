from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import structlog
import asyncio

from src.config import Timeframe
from src.engine.orchestrator import Orchestrator


def test_primary_persisted_bar_invokes_adapter_once_with_source_timestamp() -> None:
    bar = SimpleNamespace(
        ts=1_800_000_000_000,
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=12.0,
    )
    storage = SimpleNamespace(
        latest_bar_ts=AsyncMock(return_value=bar.ts),
        fetch_bars=AsyncMock(return_value=[bar]),
    )
    adapter = SimpleNamespace(on_bar=AsyncMock())
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._intelligence_adapter = adapter
    orchestrator._last_intelligence_bar_ts = None
    orchestrator._intelligence_bar_lock = asyncio.Lock()
    orchestrator._storage = storage
    orchestrator._symbol = "BTC/USDT"
    orchestrator._log = structlog.get_logger("test.intelligence")

    import asyncio

    asyncio.run(orchestrator._process_intelligence_bar(Timeframe.INTRADAY))
    asyncio.run(orchestrator._process_intelligence_bar(Timeframe.INTRADAY))

    adapter.on_bar.assert_awaited_once()
    call = adapter.on_bar.await_args.kwargs
    assert call["bar_ts_ms"] == bar.ts
    assert call["ohlcv"]["timestamp"] == bar.ts
    assert call["ohlcv"]["close"] == 101.0
    storage.fetch_bars.assert_awaited_once()
