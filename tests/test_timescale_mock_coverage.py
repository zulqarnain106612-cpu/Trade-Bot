"""
Mock-based coverage for TimescaleBackend — tests that do not require a live DB.

Covers: __init__, _get_lock, _require_pool, close, upsert_bars (empty path),
fetch_bars, latest_bar_ts, latest_close, bar_count, insert_trade,
update_trade_exit, fetch_trades, insert_model_metrics, latest_model_metrics,
live_gate_passes, insert_equity, fetch_equity_curve, latest_equity,
earliest_equity_ts, validate_symbol, insert_audit_event, health_check,
insert_missed_trade, fetch_missed_trades, upsert_regime_snapshot,
latest_regime, bars_before, daily_pnl, count_consecutive_losses,
store_intelligence_features, fetch_intelligence_features,
intelligence_feature_coverage, regime_snapshot_before, prune_old_bars,
open_timescale_storage context manager.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data.storage import (
    BarRecord,
    EquityRecord,
    MissedTradeRecord,
    ModelMetricsRecord,
    RegimeSnapshotRecord,
    TradeRecord,
)
from src.data.timescale_storage import (
    TimescaleBackend,
    _rows_from_status,
    open_timescale_storage,
)


_TEST_DSN = "postgresql://user:pw@host/db"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend(dsn: str = _TEST_DSN) -> TimescaleBackend:
    """Return a TimescaleBackend with a mocked pool already attached."""
    b = TimescaleBackend(dsn=dsn)
    pool = _mock_pool()
    b._pool = pool
    return b


def _mock_pool() -> MagicMock:
    """Return a MagicMock asyncpg pool whose acquire() is an async ctx manager."""
    pool = MagicMock()
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 3")
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    pool.close = AsyncMock()
    return pool


def _make_bar() -> BarRecord:
    return BarRecord(
        symbol="BTC/USDT",
        timeframe="1h",
        ts=1_700_000_000_000,
        open=30000.0,
        high=31000.0,
        low=29000.0,
        close=30500.0,
        volume=100.0,
        quote_volume=3_000_000.0,
        taker_buy_vol=50.0,
    )


def _make_trade() -> TradeRecord:
    return TradeRecord(
        id="trade-001",
        symbol="BTC/USDT",
        timeframe="1h",
        trading_mode="paper",
        execution_mode="paper",
        direction="long",
        entry_price=30000.0,
        exit_price=31000.0,
        quantity=0.1,
        notional_usd=3000.0,
        entry_ts=1_700_000_000_000,
        exit_ts=1_700_003_600_000,
        pnl_usd=100.0,
        pnl_pct=3.33,
        fee_usd=5.0,
        kelly_fraction=0.05,
        regime_at_entry=1,
        meta_label_prob=0.8,
        exit_reason="tp",
        approved_by="auto",
        raw_signal={"score": 0.9},
    )


# ---------------------------------------------------------------------------
# _rows_from_status
# ---------------------------------------------------------------------------


class TestRowsFromStatus:
    def test_insert_status(self):
        assert _rows_from_status("INSERT 0 3") == 3

    def test_delete_status(self):
        assert _rows_from_status("DELETE 2") == 2

    def test_update_status(self):
        assert _rows_from_status("UPDATE 1") == 1

    def test_empty_string(self):
        assert _rows_from_status("") == 0

    def test_non_numeric(self):
        assert _rows_from_status("OK done") == 0


# ---------------------------------------------------------------------------
# TimescaleBackend.__init__ and _get_lock
# ---------------------------------------------------------------------------


class TestInit:
    def test_dsn_stored(self):
        b = TimescaleBackend(dsn=_TEST_DSN)
        assert "host/db" in b._dsn

    def test_pool_none_on_init(self):
        b = TimescaleBackend(dsn=_TEST_DSN)
        assert b._pool is None

    def test_get_lock_creates_once(self):
        b = TimescaleBackend(dsn=_TEST_DSN)

        # Need an event loop for asyncio.Lock
        async def _run():
            lock1 = b._get_lock()
            lock2 = b._get_lock()
            assert lock1 is lock2

        asyncio.run(_run())

    def test_get_lock_returns_existing(self):
        b = TimescaleBackend(dsn=_TEST_DSN)

        async def _run():
            lock = asyncio.Lock()
            b._lock = lock
            assert b._get_lock() is lock

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# _require_pool
# ---------------------------------------------------------------------------


class TestRequirePool:
    def test_raises_when_not_initialized(self):
        b = TimescaleBackend(dsn=_TEST_DSN)
        with pytest.raises(RuntimeError, match="not initialized"):
            b._require_pool()

    def test_returns_pool_when_set(self):
        b = _make_backend()
        pool = b._require_pool()
        assert pool is b._pool


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_close_when_pool_none_is_noop(self):
        b = TimescaleBackend(dsn=_TEST_DSN)
        b._lock = asyncio.Lock()
        await b.close()  # should not raise

    @pytest.mark.asyncio
    async def test_close_calls_pool_close(self):
        b = _make_backend()
        b._lock = asyncio.Lock()
        pool = b._pool
        await b.close()
        pool.close.assert_called_once()
        assert b._pool is None


# ---------------------------------------------------------------------------
# upsert_bars
# ---------------------------------------------------------------------------


class TestUpsertBars:
    @pytest.mark.asyncio
    async def test_empty_list_returns_zero(self):
        b = _make_backend()
        result = await b.upsert_bars([])
        assert result == 0

    @pytest.mark.asyncio
    async def test_inserts_bars(self):
        b = _make_backend()
        bar = _make_bar()
        b._pool.acquire().__aenter__ = AsyncMock()
        result = await b.upsert_bars([bar])
        assert result == 3  # mocked status "INSERT 0 3"


# ---------------------------------------------------------------------------
# fetch_bars
# ---------------------------------------------------------------------------


class TestFetchBars:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_rows(self):
        b = _make_backend()
        rows = await b.fetch_bars("BTC/USDT", "1h", 0)
        assert rows == []

    @pytest.mark.asyncio
    async def test_maps_rows_to_bar_records(self):
        b = _make_backend()
        mock_row = {
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "ts": 1_700_000_000_000,
            "open": 30000.0,
            "high": 31000.0,
            "low": 29000.0,
            "close": 30500.0,
            "volume": 100.0,
            "quote_volume": 3_000_000.0,
            "taker_buy_vol": 50.0,
        }

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.fetch = AsyncMock(return_value=[mock_row])
            yield conn

        b._pool.acquire = _acquire
        rows = await b.fetch_bars("BTC/USDT", "1h", 0)
        assert len(rows) == 1
        assert rows[0].close == 30500.0


# ---------------------------------------------------------------------------
# latest_bar_ts
# ---------------------------------------------------------------------------


class TestLatestBarTs:
    @pytest.mark.asyncio
    async def test_none_when_no_bars(self):
        b = _make_backend()
        result = await b.latest_bar_ts("BTC/USDT", "1h")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_int_ts(self):
        b = _make_backend()

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=1_700_000_000_000)
            yield conn

        b._pool.acquire = _acquire
        result = await b.latest_bar_ts("BTC/USDT", "1h")
        assert result == 1_700_000_000_000


# ---------------------------------------------------------------------------
# latest_close
# ---------------------------------------------------------------------------


class TestLatestClose:
    @pytest.mark.asyncio
    async def test_none_when_no_row(self):
        b = _make_backend()
        result = await b.latest_close("BTC/USDT", "1h")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_tuple(self):
        b = _make_backend()

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.fetchrow = AsyncMock(return_value={"ts": 1_700_000_000_000, "close": 30500.0})
            yield conn

        b._pool.acquire = _acquire
        result = await b.latest_close("BTC/USDT", "1h")
        assert result == (1_700_000_000_000, 30500.0)

    @pytest.mark.asyncio
    async def test_none_when_row_ts_none(self):
        b = _make_backend()

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.fetchrow = AsyncMock(return_value={"ts": None, "close": 30500.0})
            yield conn

        b._pool.acquire = _acquire
        result = await b.latest_close("BTC/USDT", "1h")
        assert result is None


# ---------------------------------------------------------------------------
# bar_count
# ---------------------------------------------------------------------------


class TestBarCount:
    @pytest.mark.asyncio
    async def test_returns_zero_on_none(self):
        b = _make_backend()
        result = await b.bar_count("BTC/USDT", "1h")
        assert result == 0

    @pytest.mark.asyncio
    async def test_returns_count(self):
        b = _make_backend()

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=42)
            yield conn

        b._pool.acquire = _acquire
        result = await b.bar_count("BTC/USDT", "1h")
        assert result == 42


# ---------------------------------------------------------------------------
# prune_old_bars
# ---------------------------------------------------------------------------


class TestPruneOldBars:
    @pytest.mark.asyncio
    async def test_returns_rows_pruned(self):
        b = _make_backend()

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.execute = AsyncMock(return_value="DELETE 5")
            yield conn

        b._pool.acquire = _acquire
        result = await b.prune_old_bars("BTC/USDT", "1h", keep_days=30)
        assert result == 5


# ---------------------------------------------------------------------------
# insert_trade / update_trade_exit
# ---------------------------------------------------------------------------


class TestTrades:
    @pytest.mark.asyncio
    async def test_insert_trade(self):
        b = _make_backend()
        trade = _make_trade()
        await b.insert_trade(trade)

    @pytest.mark.asyncio
    async def test_insert_trade_duplicate_raises(self):
        import asyncpg

        b = _make_backend()
        trade = _make_trade()

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.execute = AsyncMock(
                side_effect=asyncpg.UniqueViolationError("dup", "23505", None, None, None, None)
            )
            yield conn

        b._pool.acquire = _acquire
        with pytest.raises(ValueError, match="already exists"):
            await b.insert_trade(trade)

    @pytest.mark.asyncio
    async def test_update_trade_exit(self):
        b = _make_backend()
        await b.update_trade_exit(
            trade_id="trade-001",
            exit_price=31000.0,
            exit_ts=1_700_003_600_000,
            pnl_usd=100.0,
            pnl_pct=3.33,
            exit_reason="tp",
            fee_usd=5.0,
        )


# ---------------------------------------------------------------------------
# fetch_trades
# ---------------------------------------------------------------------------


class TestFetchTrades:
    @pytest.mark.asyncio
    async def test_returns_empty(self):
        b = _make_backend()
        result = await b.fetch_trades("BTC/USDT", "paper", limit=10)
        assert result == []


# ---------------------------------------------------------------------------
# count_consecutive_losses / daily_pnl
# ---------------------------------------------------------------------------


class TestRiskMetrics:
    @pytest.mark.asyncio
    async def test_count_consecutive_losses_none(self):
        b = _make_backend()

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.fetch = AsyncMock(return_value=[])
            yield conn

        b._pool.acquire = _acquire
        result = await b.count_consecutive_losses("BTC/USDT", "paper")
        assert result == 0

    @pytest.mark.asyncio
    async def test_daily_pnl_none(self):
        b = _make_backend()

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=None)
            yield conn

        b._pool.acquire = _acquire
        result = await b.daily_pnl("BTC/USDT", "paper")
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_daily_pnl_returns_float(self):
        b = _make_backend()

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=250.5)
            yield conn

        b._pool.acquire = _acquire
        result = await b.daily_pnl("BTC/USDT", "paper")
        assert result == 250.5


# ---------------------------------------------------------------------------
# upsert_regime_snapshot / latest_regime
# ---------------------------------------------------------------------------


class TestRegime:
    @pytest.mark.asyncio
    async def test_upsert_regime_snapshot(self):
        b = _make_backend()
        snap = RegimeSnapshotRecord(
            symbol="BTC/USDT",
            timeframe="1h",
            ts=1_700_000_000_000,
            regime_state=1,
            prob_ranging=0.1,
            prob_trending=0.7,
            prob_volatile=0.2,
        )
        await b.upsert_regime_snapshot(snap)

    @pytest.mark.asyncio
    async def test_latest_regime_none(self):
        b = _make_backend()
        result = await b.latest_regime("BTC/USDT", "1h")
        assert result is None


# ---------------------------------------------------------------------------
# model_metrics
# ---------------------------------------------------------------------------


class TestModelMetrics:
    @pytest.mark.asyncio
    async def test_insert_model_metrics(self):
        b = _make_backend()
        metrics = ModelMetricsRecord(
            model_name="direction",
            timeframe="1h",
            version="v1",
            oos_sharpe=1.2,
            max_drawdown=0.05,
            n_trades=100,
            accuracy=0.65,
            precision_score=0.64,
            recall_score=0.63,
            f1_score=0.63,
            live_gate_pass=True,
        )
        await b.insert_model_metrics(metrics)

    @pytest.mark.asyncio
    async def test_latest_model_metrics_none(self):
        b = _make_backend()
        result = await b.latest_model_metrics("direction", "1h")
        assert result is None

    @pytest.mark.asyncio
    async def test_live_gate_passes_false_on_none(self):
        b = _make_backend()
        result = await b.live_gate_passes("1h")
        assert result is False


# ---------------------------------------------------------------------------
# equity
# ---------------------------------------------------------------------------


class TestEquity:
    @pytest.mark.asyncio
    async def test_insert_equity(self):
        b = _make_backend()
        rec = EquityRecord(
            ts=1_700_000_000_000,
            trading_mode="paper",
            equity_usd=10000.0,
            cash_usd=10000.0,
            unrealized_pnl=0.0,
            daily_pnl_usd=0.0,
            daily_pnl_pct=0.0,
            peak_equity_usd=10000.0,
            drawdown_pct=0.0,
        )
        await b.insert_equity(rec)

    @pytest.mark.asyncio
    async def test_fetch_equity_curve_empty(self):
        b = _make_backend()
        result = await b.fetch_equity_curve("paper", limit=100)
        assert result == []

    @pytest.mark.asyncio
    async def test_latest_equity_none(self):
        b = _make_backend()
        result = await b.latest_equity("paper")
        assert result is None

    @pytest.mark.asyncio
    async def test_earliest_equity_ts_none(self):
        b = _make_backend()
        result = await b.earliest_equity_ts("paper")
        assert result is None


# ---------------------------------------------------------------------------
# validate_symbol
# ---------------------------------------------------------------------------


class TestValidateSymbol:
    @pytest.mark.asyncio
    async def test_valid_symbol_no_raise(self):
        b = _make_backend()

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=5)
            yield conn

        b._pool.acquire = _acquire
        await b.validate_symbol("BTC/USDT")

    @pytest.mark.asyncio
    async def test_no_bars_raises(self):
        b = _make_backend()

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=0)
            yield conn

        b._pool.acquire = _acquire
        with pytest.raises(ValueError, match="Unknown symbol"):
            await b.validate_symbol("BTC/USDT")


# ---------------------------------------------------------------------------
# insert_audit_event
# ---------------------------------------------------------------------------


class TestAuditEvent:
    @pytest.mark.asyncio
    async def test_insert_audit_event(self):
        b = _make_backend()
        await b.insert_audit_event(
            event_type="test",
            operator="system",
            details={"key": "value"},
        )


# ---------------------------------------------------------------------------
# insert_missed_trade / fetch_missed_trades
# ---------------------------------------------------------------------------


class TestMissedTrades:
    @pytest.mark.asyncio
    async def test_insert_missed_trade(self):
        b = _make_backend()
        rec = MissedTradeRecord(
            id="missed-001",
            symbol="BTC/USDT",
            timeframe="1h",
            ts=1_700_000_000_000,
            direction=1,
            reason="risk_veto",
            kelly_fraction=0.05,
            meta_label_prob=0.8,
            raw_signal=0.9,
            regime_at_entry=1,
            notional_usd=3000.0,
        )
        await b.insert_missed_trade(rec)

    @pytest.mark.asyncio
    async def test_fetch_missed_trades_empty(self):
        b = _make_backend()
        result = await b.fetch_missed_trades("BTC/USDT", limit=10)
        assert result == []


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check_returns_dict(self):
        b = _make_backend()

        call_count = 0

        @asynccontextmanager
        async def _acquire():
            nonlocal call_count
            call_count += 1
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=10)
            yield conn

        b._pool.acquire = _acquire
        result = await b.health_check()
        assert isinstance(result, dict)
        assert all(isinstance(v, int) for v in result.values())


# ---------------------------------------------------------------------------
# bars_before
# ---------------------------------------------------------------------------


class TestBarsBefore:
    @pytest.mark.asyncio
    async def test_returns_empty(self):
        b = _make_backend()
        result = await b.bars_before("BTC/USDT", "1h", 1_700_000_000_000, limit=10)
        assert result == []


# ---------------------------------------------------------------------------
# regime_snapshot_before
# ---------------------------------------------------------------------------


class TestRegimeSnapshotBefore:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_row(self):
        b = _make_backend()
        result = await b.regime_snapshot_before("BTC/USDT", "1h", 1_700_000_000_000)
        assert result is None


# ---------------------------------------------------------------------------
# store_intelligence_features / fetch_intelligence_features /
# intelligence_feature_coverage
# ---------------------------------------------------------------------------


class TestIntelligenceFeatures:
    @pytest.mark.asyncio
    async def test_store_intelligence_features(self):
        b = _make_backend()
        await b.store_intelligence_features(
            symbol="BTC/USDT",
            timeframe="1h",
            bar_ts=1_700_000_000_000,
            features={"vol_ratio": 1.2, "rsi_14": 55.0},
            confidence=0.85,
            source="live",
        )

    @pytest.mark.asyncio
    async def test_fetch_intelligence_features_empty(self):
        b = _make_backend()
        result = await b.fetch_intelligence_features("BTC/USDT", "1h")
        assert result.empty

    @pytest.mark.asyncio
    async def test_fetch_intelligence_features_one_row(self):
        from src.data.timescale_storage import _INTEL_COLUMNS

        b = _make_backend()
        row = {"bar_ts": 1_700_000_000_000, "confidence": 0.9, **{c: 1.0 for c in _INTEL_COLUMNS}}

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.fetch = AsyncMock(return_value=[row])
            yield conn

        b._pool.acquire = _acquire
        df = await b.fetch_intelligence_features("BTC/USDT", "1h")
        assert not df.empty
        assert df.index.name == "bar_ts"
        assert "intelligence_exchange_netflow_7d_zscore" in df.columns

    @pytest.mark.asyncio
    async def test_intelligence_feature_coverage_no_rows(self):
        b = _make_backend()

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.fetchrow = AsyncMock(return_value={"total": 0})
            yield conn

        b._pool.acquire = _acquire
        result = await b.intelligence_feature_coverage("BTC/USDT", "1h")
        assert result == {"total_rows": 0, "coverage": {}}

    @pytest.mark.asyncio
    async def test_intelligence_feature_coverage_with_rows(self):
        from src.data.timescale_storage import _INTEL_COLUMNS

        b = _make_backend()
        row = {"total": 10, **{c: 8 for c in _INTEL_COLUMNS}}

        @asynccontextmanager
        async def _acquire():
            conn = AsyncMock()
            conn.fetchrow = AsyncMock(return_value=row)
            yield conn

        b._pool.acquire = _acquire
        result = await b.intelligence_feature_coverage("BTC/USDT", "1h")
        assert result["total_rows"] == 10
        assert result["coverage"]["intelligence_exchange_netflow_7d_zscore"] == 0.8


# ---------------------------------------------------------------------------
# open_timescale_storage context manager
# ---------------------------------------------------------------------------


class TestOpenTimescaleStorage:
    @pytest.mark.asyncio
    async def test_yields_and_closes(self):
        with patch("src.data.timescale_storage.TimescaleBackend") as MockBackend:
            mock_instance = AsyncMock()
            mock_instance.initialize = AsyncMock()
            mock_instance.close = AsyncMock()
            MockBackend.return_value = mock_instance

            async with open_timescale_storage(dsn=_TEST_DSN) as storage:
                assert storage is mock_instance

            mock_instance.initialize.assert_called_once()
            mock_instance.close.assert_called_once()
