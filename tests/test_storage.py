"""Test coverage for src/data/storage.py — async SQLite storage backend."""

import os
import tempfile

import pytest

from src.data.storage import (
    BarRecord,
    EquityRecord,
    ModelMetricsRecord,
    RegimeSnapshotRecord,
    StorageBackend,
    TradeRecord,
    open_storage,
)


@pytest.fixture
async def backend():
    """Provide an initialized StorageBackend on a temp file, closed after test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # let sqlite create fresh
    sb = StorageBackend(db_path=path)
    await sb.initialize()
    yield sb
    await sb.close()
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except FileNotFoundError:
            pass


def make_bar(symbol="BTC/USDT", timeframe="15m", ts=1000, close=100.0):
    return BarRecord(
        symbol=symbol, timeframe=timeframe, ts=ts,
        open=close - 1, high=close + 1, low=close - 2, close=close,
        volume=10.0, quote_volume=1000.0, taker_buy_vol=5.0,
    )


def make_trade(trade_id="t1", symbol="BTC/USDT", entry_ts=1000, pnl_usd=None, exit_ts=None):
    return TradeRecord(
        id=trade_id, symbol=symbol, timeframe="15m", trading_mode="paper",
        execution_mode="paper", direction=1, entry_price=100.0, exit_price=None,
        quantity=1.0, notional_usd=100.0, entry_ts=entry_ts, exit_ts=exit_ts,
        pnl_usd=pnl_usd, pnl_pct=None, fee_usd=0.1, kelly_fraction=0.1,
        regime_at_entry=1, meta_label_prob=0.6, exit_reason=None,
        approved_by="system", raw_signal=0.5,
    )


def make_regime(symbol="BTC/USDT", timeframe="15m", ts=1000, state=1):
    return RegimeSnapshotRecord(
        symbol=symbol, timeframe=timeframe, ts=ts, regime_state=state,
        prob_ranging=0.2, prob_trending=0.7, prob_volatile=0.1,
    )


def make_metrics(model_name="direction", timeframe="15m", version="v1", gate_pass=True):
    return ModelMetricsRecord(
        model_name=model_name, timeframe=timeframe, version=version,
        oos_sharpe=1.5, max_drawdown=0.1, n_trades=50, accuracy=0.6,
        precision_score=0.65, recall_score=0.55, f1_score=0.6,
        live_gate_pass=gate_pass,
    )


def make_equity(ts=1000, trading_mode="paper", equity=10000.0):
    return EquityRecord(
        ts=ts, trading_mode=trading_mode, equity_usd=equity, cash_usd=5000.0,
        unrealized_pnl=0.0, daily_pnl_usd=0.0, daily_pnl_pct=0.0,
        peak_equity_usd=equity, drawdown_pct=0.0,
    )


class TestRecordConstructors:
    """Lightweight dataclass-like record construction."""

    def test_bar_record_defaults(self):
        bar = BarRecord(symbol="BTC/USDT", timeframe="15m", ts=1, open=1, high=2, low=0, close=1, volume=10)
        assert bar.quote_volume == 0.0
        assert bar.taker_buy_vol == 0.0

    def test_trade_record_all_fields(self):
        t = make_trade()
        assert t.id == "t1"
        assert t.direction == 1

    def test_regime_snapshot_record(self):
        r = make_regime()
        assert r.regime_state == 1

    def test_model_metrics_record(self):
        m = make_metrics()
        assert m.live_gate_pass is True

    def test_equity_record(self):
        e = make_equity()
        assert e.equity_usd == 10000.0


class TestInitializeAndClose:
    """Lifecycle: initialize, close, double-close, uninitialized access."""

    @pytest.mark.asyncio
    async def test_initialize_creates_tables(self, backend):
        health = await backend.health_check()
        assert "bars" in health
        assert health["bars"] == 0

    @pytest.mark.asyncio
    async def test_double_initialize_is_idempotent(self, backend):
        await backend.initialize()  # second call should no-op
        health = await backend.health_check()
        assert health["bars"] == 0

    @pytest.mark.asyncio
    async def test_require_conn_raises_before_init(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        sb = StorageBackend(db_path=path)
        with pytest.raises(RuntimeError, match="not initialized"):
            sb._require_conn()

    @pytest.mark.asyncio
    async def test_close_on_uninitialized_is_safe(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        sb = StorageBackend(db_path=path)
        await sb.close()  # should not raise


class TestOpenStorageContextManager:
    """open_storage() async context manager."""

    @pytest.mark.asyncio
    async def test_open_storage_initializes_and_closes(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        async with open_storage(db_path=path) as storage:
            health = await storage.health_check()
            assert "bars" in health
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(path + ext)
            except FileNotFoundError:
                pass


class TestBars:
    """upsert_bars, fetch_bars, latest_bar_ts, bar_count, prune_old_bars."""

    @pytest.mark.asyncio
    async def test_upsert_bars_inserts_new(self, backend):
        bars = [make_bar(ts=1000), make_bar(ts=2000)]
        inserted = await backend.upsert_bars(bars)
        assert inserted == 2

    @pytest.mark.asyncio
    async def test_upsert_bars_empty_list_returns_zero(self, backend):
        inserted = await backend.upsert_bars([])
        assert inserted == 0

    @pytest.mark.asyncio
    async def test_upsert_bars_duplicate_ignored(self, backend):
        bar = make_bar(ts=1000)
        await backend.upsert_bars([bar])
        inserted_again = await backend.upsert_bars([bar])
        assert inserted_again == 0

    @pytest.mark.asyncio
    async def test_fetch_bars_returns_ordered(self, backend):
        await backend.upsert_bars([make_bar(ts=3000), make_bar(ts=1000), make_bar(ts=2000)])
        rows = await backend.fetch_bars("BTC/USDT", "15m", since_ts=0)
        assert [r.ts for r in rows] == [1000, 2000, 3000]

    @pytest.mark.asyncio
    async def test_fetch_bars_respects_since_ts(self, backend):
        await backend.upsert_bars([make_bar(ts=1000), make_bar(ts=2000), make_bar(ts=3000)])
        rows = await backend.fetch_bars("BTC/USDT", "15m", since_ts=2000)
        assert [r.ts for r in rows] == [2000, 3000]

    @pytest.mark.asyncio
    async def test_fetch_bars_respects_limit(self, backend):
        await backend.upsert_bars([make_bar(ts=t) for t in range(10)])
        rows = await backend.fetch_bars("BTC/USDT", "15m", since_ts=0, limit=3)
        assert len(rows) == 3

    @pytest.mark.asyncio
    async def test_fetch_bars_empty_when_no_match(self, backend):
        rows = await backend.fetch_bars("ETH/USDT", "15m", since_ts=0)
        assert rows == []

    @pytest.mark.asyncio
    async def test_latest_bar_ts_returns_max(self, backend):
        await backend.upsert_bars([make_bar(ts=1000), make_bar(ts=5000)])
        ts = await backend.latest_bar_ts("BTC/USDT", "15m")
        assert ts == 5000

    @pytest.mark.asyncio
    async def test_latest_bar_ts_none_when_empty(self, backend):
        ts = await backend.latest_bar_ts("BTC/USDT", "15m")
        assert ts is None

    @pytest.mark.asyncio
    async def test_bar_count(self, backend):
        await backend.upsert_bars([make_bar(ts=t) for t in range(5)])
        count = await backend.bar_count("BTC/USDT", "15m")
        assert count == 5

    @pytest.mark.asyncio
    async def test_bar_count_zero_when_empty(self, backend):
        count = await backend.bar_count("BTC/USDT", "15m")
        assert count == 0

    @pytest.mark.asyncio
    async def test_prune_old_bars_deletes_old(self, backend):
        old_ts = 1  # epoch ms, ancient
        await backend.upsert_bars([make_bar(ts=old_ts)])
        deleted = await backend.prune_old_bars("BTC/USDT", "15m", keep_days=30)
        assert deleted == 1

    @pytest.mark.asyncio
    async def test_prune_old_bars_keeps_recent(self, backend):
        import time
        recent_ts = int(time.time() * 1000)
        await backend.upsert_bars([make_bar(ts=recent_ts)])
        deleted = await backend.prune_old_bars("BTC/USDT", "15m", keep_days=30)
        assert deleted == 0
        count = await backend.bar_count("BTC/USDT", "15m")
        assert count == 1


class TestTrades:
    """insert_trade, update_trade_exit, fetch_trades, consecutive losses, daily pnl."""

    @pytest.mark.asyncio
    async def test_insert_trade(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1"))
        rows = await backend.fetch_trades()
        assert len(rows) == 1
        assert rows[0].id == "t1"

    @pytest.mark.asyncio
    async def test_insert_duplicate_trade_raises(self, backend):
        await backend.insert_trade(make_trade(trade_id="dup"))
        with pytest.raises(ValueError, match="already exists"):
            await backend.insert_trade(make_trade(trade_id="dup"))

    @pytest.mark.asyncio
    async def test_update_trade_exit_success(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1"))
        await backend.update_trade_exit(
            trade_id="t1", exit_price=110.0, exit_ts=2000,
            pnl_usd=10.0, pnl_pct=0.1, exit_reason="tp", fee_usd=0.05,
        )
        rows = await backend.fetch_trades()
        assert rows[0].exit_price == 110.0
        assert rows[0].pnl_usd == 10.0

    @pytest.mark.asyncio
    async def test_update_trade_exit_accumulates_fee(self, backend):
        trade = make_trade(trade_id="t1")
        trade.fee_usd = 0.1
        await backend.insert_trade(trade)
        await backend.update_trade_exit(
            trade_id="t1", exit_price=110.0, exit_ts=2000,
            pnl_usd=10.0, pnl_pct=0.1, exit_reason="tp", fee_usd=0.05,
        )
        rows = await backend.fetch_trades()
        assert rows[0].fee_usd == pytest.approx(0.15)

    @pytest.mark.asyncio
    async def test_update_trade_exit_nonexistent_raises(self, backend):
        with pytest.raises(ValueError, match="No open trade found"):
            await backend.update_trade_exit(
                trade_id="ghost", exit_price=110.0, exit_ts=2000,
                pnl_usd=10.0, pnl_pct=0.1, exit_reason="tp", fee_usd=0.05,
            )

    @pytest.mark.asyncio
    async def test_update_trade_exit_already_closed_raises(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1"))
        await backend.update_trade_exit(
            trade_id="t1", exit_price=110.0, exit_ts=2000,
            pnl_usd=10.0, pnl_pct=0.1, exit_reason="tp", fee_usd=0.05,
        )
        with pytest.raises(ValueError, match="already closed"):
            await backend.update_trade_exit(
                trade_id="t1", exit_price=120.0, exit_ts=3000,
                pnl_usd=20.0, pnl_pct=0.2, exit_reason="tp", fee_usd=0.05,
            )

    @pytest.mark.asyncio
    async def test_fetch_trades_filter_by_symbol(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1", symbol="BTC/USDT"))
        await backend.insert_trade(make_trade(trade_id="t2", symbol="ETH/USDT"))
        rows = await backend.fetch_trades(symbol="BTC/USDT")
        assert len(rows) == 1
        assert rows[0].symbol == "BTC/USDT"

    @pytest.mark.asyncio
    async def test_fetch_trades_filter_by_trading_mode(self, backend):
        t1 = make_trade(trade_id="t1")
        t1.trading_mode = "live"
        await backend.insert_trade(t1)
        await backend.insert_trade(make_trade(trade_id="t2"))  # paper
        rows = await backend.fetch_trades(trading_mode="live")
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_fetch_trades_filter_by_since_ts(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1", entry_ts=1000))
        await backend.insert_trade(make_trade(trade_id="t2", entry_ts=5000))
        rows = await backend.fetch_trades(since_ts=3000)
        assert len(rows) == 1
        assert rows[0].id == "t2"

    @pytest.mark.asyncio
    async def test_fetch_trades_limit_and_offset(self, backend):
        for i in range(5):
            await backend.insert_trade(make_trade(trade_id=f"t{i}", entry_ts=1000 + i))
        rows = await backend.fetch_trades(limit=2, offset=1)
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_fetch_trades_ordered_descending(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1", entry_ts=1000))
        await backend.insert_trade(make_trade(trade_id="t2", entry_ts=3000))
        await backend.insert_trade(make_trade(trade_id="t3", entry_ts=2000))
        rows = await backend.fetch_trades()
        assert [r.id for r in rows] == ["t2", "t3", "t1"]

    @pytest.mark.asyncio
    async def test_count_consecutive_losses_all_losses(self, backend):
        for i in range(3):
            t = make_trade(trade_id=f"t{i}", entry_ts=1000 + i, pnl_usd=-5.0)
            await backend.insert_trade(t)
        streak = await backend.count_consecutive_losses("BTC/USDT", "paper")
        assert streak == 3

    @pytest.mark.asyncio
    async def test_count_consecutive_losses_stops_at_win(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1", entry_ts=1000, pnl_usd=-5.0))
        await backend.insert_trade(make_trade(trade_id="t2", entry_ts=2000, pnl_usd=10.0))
        await backend.insert_trade(make_trade(trade_id="t3", entry_ts=3000, pnl_usd=-5.0))
        streak = await backend.count_consecutive_losses("BTC/USDT", "paper")
        assert streak == 1  # most recent (t3) is a loss, then t2 win stops it

    @pytest.mark.asyncio
    async def test_count_consecutive_losses_no_trades(self, backend):
        streak = await backend.count_consecutive_losses("BTC/USDT", "paper")
        assert streak == 0

    @pytest.mark.asyncio
    async def test_daily_pnl_sums_todays_trades(self, backend):
        import time
        now_ms = int(time.time() * 1000)
        t = make_trade(trade_id="t1", entry_ts=now_ms, pnl_usd=15.0, exit_ts=now_ms)
        await backend.insert_trade(t)
        pnl = await backend.daily_pnl("BTC/USDT", "paper")
        assert pnl == pytest.approx(15.0)

    @pytest.mark.asyncio
    async def test_daily_pnl_zero_when_no_trades(self, backend):
        pnl = await backend.daily_pnl("BTC/USDT", "paper")
        assert pnl == 0.0

    @pytest.mark.asyncio
    async def test_daily_pnl_excludes_old_trades(self, backend):
        old_ts = 1000  # epoch ms, ancient — not today
        t = make_trade(trade_id="t1", entry_ts=old_ts, pnl_usd=15.0, exit_ts=old_ts)
        await backend.insert_trade(t)
        pnl = await backend.daily_pnl("BTC/USDT", "paper")
        assert pnl == 0.0


class TestRegimeSnapshots:
    """upsert_regime_snapshot, latest_regime."""

    @pytest.mark.asyncio
    async def test_upsert_and_fetch_latest_regime(self, backend):
        await backend.upsert_regime_snapshot(make_regime(ts=1000, state=0))
        await backend.upsert_regime_snapshot(make_regime(ts=2000, state=1))
        latest = await backend.latest_regime("BTC/USDT", "15m")
        assert latest.ts == 2000
        assert latest.regime_state == 1

    @pytest.mark.asyncio
    async def test_latest_regime_none_when_empty(self, backend):
        latest = await backend.latest_regime("BTC/USDT", "15m")
        assert latest is None

    @pytest.mark.asyncio
    async def test_upsert_regime_replaces_same_ts(self, backend):
        await backend.upsert_regime_snapshot(make_regime(ts=1000, state=0))
        await backend.upsert_regime_snapshot(make_regime(ts=1000, state=2))
        latest = await backend.latest_regime("BTC/USDT", "15m")
        assert latest.regime_state == 2


class TestModelMetrics:
    """insert_model_metrics, latest_model_metrics, live_gate_passes."""

    @pytest.mark.asyncio
    async def test_insert_and_fetch_latest_metrics(self, backend):
        await backend.insert_model_metrics(make_metrics(version="v1"))
        latest = await backend.latest_model_metrics("direction", "15m")
        assert latest.version == "v1"
        assert latest.live_gate_pass is True

    @pytest.mark.asyncio
    async def test_latest_model_metrics_none_when_empty(self, backend):
        latest = await backend.latest_model_metrics("direction", "15m")
        assert latest is None

    @pytest.mark.asyncio
    async def test_live_gate_passes_true_when_both_pass(self, backend):
        await backend.insert_model_metrics(make_metrics(model_name="direction", gate_pass=True))
        await backend.insert_model_metrics(make_metrics(model_name="meta_label", gate_pass=True))
        assert await backend.live_gate_passes("15m") is True

    @pytest.mark.asyncio
    async def test_live_gate_passes_false_when_one_fails(self, backend):
        await backend.insert_model_metrics(make_metrics(model_name="direction", gate_pass=True))
        await backend.insert_model_metrics(make_metrics(model_name="meta_label", gate_pass=False))
        assert await backend.live_gate_passes("15m") is False

    @pytest.mark.asyncio
    async def test_live_gate_passes_false_when_missing(self, backend):
        await backend.insert_model_metrics(make_metrics(model_name="direction", gate_pass=True))
        assert await backend.live_gate_passes("15m") is False

    @pytest.mark.asyncio
    async def test_live_gate_passes_false_when_no_data(self, backend):
        assert await backend.live_gate_passes("15m") is False


class TestEquityCurve:
    """insert_equity, fetch_equity_curve, latest_equity, earliest_equity_ts."""

    @pytest.mark.asyncio
    async def test_insert_and_fetch_equity_curve(self, backend):
        await backend.insert_equity(make_equity(ts=1000, equity=10000.0))
        await backend.insert_equity(make_equity(ts=2000, equity=10500.0))
        curve = await backend.fetch_equity_curve("paper")
        assert [e.ts for e in curve] == [1000, 2000]

    @pytest.mark.asyncio
    async def test_fetch_equity_curve_since_ts(self, backend):
        await backend.insert_equity(make_equity(ts=1000))
        await backend.insert_equity(make_equity(ts=2000))
        curve = await backend.fetch_equity_curve("paper", since_ts=1500)
        assert len(curve) == 1
        assert curve[0].ts == 2000

    @pytest.mark.asyncio
    async def test_fetch_equity_curve_limit(self, backend):
        for t in range(5):
            await backend.insert_equity(make_equity(ts=t))
        curve = await backend.fetch_equity_curve("paper", limit=2)
        assert len(curve) == 2

    @pytest.mark.asyncio
    async def test_latest_equity_returns_most_recent(self, backend):
        await backend.insert_equity(make_equity(ts=1000, equity=10000.0))
        await backend.insert_equity(make_equity(ts=2000, equity=10500.0))
        latest = await backend.latest_equity("paper")
        assert latest.equity_usd == 10500.0

    @pytest.mark.asyncio
    async def test_latest_equity_none_when_empty(self, backend):
        latest = await backend.latest_equity("paper")
        assert latest is None

    @pytest.mark.asyncio
    async def test_earliest_equity_ts(self, backend):
        await backend.insert_equity(make_equity(ts=3000))
        await backend.insert_equity(make_equity(ts=1000))
        earliest = await backend.earliest_equity_ts("paper")
        assert earliest == 1000

    @pytest.mark.asyncio
    async def test_earliest_equity_ts_none_when_empty(self, backend):
        earliest = await backend.earliest_equity_ts("paper")
        assert earliest is None

    @pytest.mark.asyncio
    async def test_insert_equity_upserts_same_ts(self, backend):
        await backend.insert_equity(make_equity(ts=1000, equity=10000.0))
        await backend.insert_equity(make_equity(ts=1000, equity=20000.0))
        latest = await backend.latest_equity("paper")
        assert latest.equity_usd == 20000.0


class TestValidateSymbol:
    """validate_symbol — format check then existence check."""

    @pytest.mark.asyncio
    async def test_valid_symbol_exists(self, backend):
        await backend.upsert_bars([make_bar(symbol="BTC/USDT")])
        await backend.validate_symbol("BTC/USDT")  # should not raise

    @pytest.mark.asyncio
    async def test_invalid_format_raises(self, backend):
        with pytest.raises(ValueError, match="Invalid symbol format"):
            await backend.validate_symbol("not-a-symbol")

    @pytest.mark.asyncio
    async def test_lowercase_format_raises(self, backend):
        with pytest.raises(ValueError, match="Invalid symbol format"):
            await backend.validate_symbol("btc/usdt")

    @pytest.mark.asyncio
    async def test_valid_format_but_unknown_raises(self, backend):
        with pytest.raises(ValueError, match="Unknown symbol"):
            await backend.validate_symbol("XYZ/USDT")

    @pytest.mark.asyncio
    async def test_missing_slash_raises(self, backend):
        with pytest.raises(ValueError, match="Invalid symbol format"):
            await backend.validate_symbol("BTCUSDT")


class TestAuditLog:
    """insert_audit_event."""

    @pytest.mark.asyncio
    async def test_insert_audit_event_basic(self, backend):
        await backend.insert_audit_event(event_type="mode_change", operator="admin")
        # Verify via health_check row count
        health = await backend.health_check()
        assert health["audit_log"] == 1

    @pytest.mark.asyncio
    async def test_insert_audit_event_with_details(self, backend):
        await backend.insert_audit_event(
            event_type="mode_change", operator="admin",
            details={"from": "paper", "to": "live"},
        )
        health = await backend.health_check()
        assert health["audit_log"] == 1

    @pytest.mark.asyncio
    async def test_insert_audit_event_none_details_defaults_empty(self, backend):
        await backend.insert_audit_event(event_type="startup", operator="system", details=None)
        health = await backend.health_check()
        assert health["audit_log"] == 1


class TestHealthCheck:
    """health_check — row counts across all known tables."""

    @pytest.mark.asyncio
    async def test_health_check_all_tables_present(self, backend):
        health = await backend.health_check()
        expected_tables = {"bars", "trades", "regime_snapshots", "model_metrics", "equity_curve", "audit_log"}
        assert expected_tables.issubset(set(health.keys()))

    @pytest.mark.asyncio
    async def test_health_check_reflects_inserted_data(self, backend):
        await backend.upsert_bars([make_bar(ts=1000)])
        await backend.insert_trade(make_trade(trade_id="t1"))
        health = await backend.health_check()
        assert health["bars"] == 1
        assert health["trades"] == 1
