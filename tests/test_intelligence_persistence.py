"""
Tests for persisting live intelligence features.

The engine fetched provider metrics every tick and dropped them.
`store_intelligence_features()` existed on both storage backends and was
called by exactly one thing — a hand-run backfill script — so in a live
deployment `intelligence_features_history` stayed empty, starving the
trainer's intelligence matrix and the v7 macro overlay.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from src.config import Timeframe


def _engine(storage=None):
    from src.engine.signal_engine import SignalEngine

    engine = object.__new__(SignalEngine)
    engine._symbol = "BTC/USDT"
    engine._timeframe = Timeframe.INTRADAY
    engine._storage = storage or MagicMock(store_intelligence_features=AsyncMock())
    engine._log = MagicMock()
    return engine


def _bars(last_ts: int = 1_700_000_000_000) -> pd.DataFrame:
    return pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=[last_ts - 2, last_ts - 1, last_ts])


_METRICS = {
    "binance_funding_rate_pct": 0.01,
    "exchange_stress_score": 0.4,
    "whale_buy_sell_ratio": 1.2,
    "confidence": 0.83,
}


class TestPersistence:
    @pytest.mark.asyncio
    async def test_metrics_are_written(self) -> None:
        engine = _engine()
        await engine._persist_intelligence_features(_bars(), _METRICS)
        engine._storage.store_intelligence_features.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_it_is_keyed_on_the_latest_bar(self) -> None:
        """
        Several ticks occur inside one bar; keying on the bar makes them
        upsert one row instead of accumulating duplicates.
        """
        engine = _engine()
        await engine._persist_intelligence_features(_bars(last_ts=1_699_999_999_000), _METRICS)
        kwargs = engine._storage.store_intelligence_features.await_args.kwargs
        assert kwargs["bar_ts"] == 1_699_999_999_000

    @pytest.mark.asyncio
    async def test_confidence_is_stored_in_its_own_column_not_as_a_feature(self) -> None:
        """It is the aggregator's quality score for the merge, not a feature."""
        engine = _engine()
        await engine._persist_intelligence_features(_bars(), _METRICS)
        kwargs = engine._storage.store_intelligence_features.await_args.kwargs
        assert kwargs["confidence"] == pytest.approx(0.83)
        assert "confidence" not in kwargs["features"]

    @pytest.mark.asyncio
    async def test_the_row_is_tagged_live_not_backfill(self) -> None:
        engine = _engine()
        await engine._persist_intelligence_features(_bars(), _METRICS)
        assert engine._storage.store_intelligence_features.await_args.kwargs["source"] == "live"

    @pytest.mark.asyncio
    async def test_symbol_and_timeframe_identify_the_row(self) -> None:
        engine = _engine()
        await engine._persist_intelligence_features(_bars(), _METRICS)
        kwargs = engine._storage.store_intelligence_features.await_args.kwargs
        assert kwargs["symbol"] == "BTC/USDT"
        assert kwargs["timeframe"] == Timeframe.INTRADAY.value

    @pytest.mark.asyncio
    async def test_every_non_confidence_key_is_passed_through(self) -> None:
        engine = _engine()
        await engine._persist_intelligence_features(_bars(), _METRICS)
        features = engine._storage.store_intelligence_features.await_args.kwargs["features"]
        assert features == {
            "binance_funding_rate_pct": 0.01,
            "exchange_stress_score": 0.4,
            "whale_buy_sell_ratio": 1.2,
        }


class TestNothingToWrite:
    @pytest.mark.asyncio
    async def test_empty_metrics_write_nothing(self) -> None:
        """A failed provider fetch must not write a row of nothing."""
        engine = _engine()
        await engine._persist_intelligence_features(_bars(), {})
        engine._storage.store_intelligence_features.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confidence_alone_writes_nothing(self) -> None:
        engine = _engine()
        await engine._persist_intelligence_features(_bars(), {"confidence": 0.5})
        engine._storage.store_intelligence_features.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_bars_writes_nothing(self) -> None:
        engine = _engine()
        await engine._persist_intelligence_features(pd.DataFrame(), _METRICS)
        engine._storage.store_intelligence_features.assert_not_awaited()


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_a_storage_failure_never_breaks_the_tick(self) -> None:
        """
        Losing one bar of history is recoverable by backfill; a missed
        trading decision is not.
        """
        storage = MagicMock(
            store_intelligence_features=AsyncMock(side_effect=RuntimeError("db locked"))
        )
        engine = _engine(storage)
        await engine._persist_intelligence_features(_bars(), _METRICS)  # must not raise
        engine._log.warning.assert_called_once()


def test_the_tick_actually_calls_it() -> None:
    """A writer nobody calls is the defect being fixed."""
    import inspect

    from src.engine.signal_engine import SignalEngine

    source = inspect.getsource(SignalEngine.tick)
    assert "self._persist_intelligence_features(" in source
