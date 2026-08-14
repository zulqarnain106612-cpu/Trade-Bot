"""
GAP-006: tests for TimescaleBackend — run against the live local TimescaleDB
container (scripts/timescaledb.sh). A throwaway database is created per test
session and dropped at teardown; tables are truncated between tests.

Skips the whole module if the container is not reachable.
"""

import asyncio
import os
import time
import uuid

import asyncpg
import pytest

from src.data.storage import (
    BarRecord,
    EquityRecord,
    ModelMetricsRecord,
    RegimeSnapshotRecord,
    TradeRecord,
)
from src.data.timescale_storage import (
    _PG_SCHEMA_VERSION,
    TimescaleBackend,
    _rows_from_status,
    open_timescale_storage,
)


ADMIN_DSN = os.environ.get(
    "STORAGE_TIMESCALE_DSN",
    "postgresql://tradebot:tradebot-local@127.0.0.1:5433/tradebot",  # pragma: allowlist secret
)

_ALL_TABLES = (
    "bars, trades, regime_snapshots, model_metrics, "
    "equity_curve, audit_log, intelligence_features_history, missed_trades"
)

_SKIP_MSG = "TimescaleDB container not running — bash scripts/timescaledb.sh up"


# ---------------------------------------------------------------------------
# Session fixtures — throwaway database lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def test_dsn():
    """Create a throwaway database for this session; drop it at teardown."""
    db_name = f"tradebot_test_{uuid.uuid4().hex[:8]}"

    async def _admin_execute(sql: str) -> None:
        conn = await asyncpg.connect(ADMIN_DSN)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    try:
        asyncio.run(_admin_execute(f'CREATE DATABASE "{db_name}"'))
    except (OSError, ConnectionError, asyncpg.PostgresError):
        pytest.skip(_SKIP_MSG)

    yield ADMIN_DSN.rsplit("/", 1)[0] + "/" + db_name

    asyncio.run(_admin_execute(f'DROP DATABASE "{db_name}" WITH (FORCE)'))


@pytest.fixture
async def backend(test_dsn):
    """Initialized TimescaleBackend on the throwaway DB, truncated per test."""
    b = TimescaleBackend(dsn=test_dsn)
    await b.initialize()
    async with b._pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {_ALL_TABLES}")
    yield b
    await b.close()


# ---------------------------------------------------------------------------
# Record factories — mirror tests/test_storage.py
# ---------------------------------------------------------------------------


def make_bar(symbol="BTC/USDT", timeframe="15m", ts=1000, close=100.0):
    return BarRecord(
        symbol=symbol,
        timeframe=timeframe,
        ts=ts,
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=10.0,
        quote_volume=1000.0,
        taker_buy_vol=5.0,
    )


def make_trade(trade_id="t1", symbol="BTC/USDT", entry_ts=1000, pnl_usd=None, exit_ts=None):
    return TradeRecord(
        id=trade_id,
        symbol=symbol,
        timeframe="15m",
        trading_mode="paper",
        execution_mode="paper",
        direction=1,
        entry_price=100.0,
        exit_price=None,
        quantity=1.0,
        notional_usd=100.0,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        pnl_usd=pnl_usd,
        pnl_pct=None,
        fee_usd=0.1,
        kelly_fraction=0.1,
        regime_at_entry=1,
        meta_label_prob=0.6,
        exit_reason=None,
        approved_by="system",
        raw_signal=0.5,
    )


def make_regime(symbol="BTC/USDT", timeframe="15m", ts=1000, state=1):
    return RegimeSnapshotRecord(
        symbol=symbol,
        timeframe=timeframe,
        ts=ts,
        regime_state=state,
        prob_ranging=0.2,
        prob_trending=0.7,
        prob_volatile=0.1,
    )


def make_metrics(model_name="direction", timeframe="15m", version="v1", gate_pass=True):
    return ModelMetricsRecord(
        model_name=model_name,
        timeframe=timeframe,
        version=version,
        oos_sharpe=1.5,
        max_drawdown=0.1,
        n_trades=50,
        accuracy=0.6,
        precision_score=0.65,
        recall_score=0.55,
        f1_score=0.6,
        live_gate_pass=gate_pass,
    )


def make_equity(ts=1000, trading_mode="paper", equity=10000.0):
    return EquityRecord(
        ts=ts,
        trading_mode=trading_mode,
        equity_usd=equity,
        cash_usd=5000.0,
        unrealized_pnl=0.0,
        daily_pnl_usd=0.0,
        daily_pnl_pct=0.0,
        peak_equity_usd=equity,
        drawdown_pct=0.0,
    )


def make_features(**overrides):
    features = {
        "intelligence_exchange_netflow_7d_zscore": 1.5,
        "intelligence_whale_buy_sell_ratio": 0.8,
        "intelligence_binance_funding_rate_pct": 0.01,
        "intelligence_mvrv_z_score": 2.2,
        "intelligence_sopr": 1.01,
    }
    features.update(overrides)
    return features


# ---------------------------------------------------------------------------
# Helpers (no container required)
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_rows_from_status_insert(self):
        assert _rows_from_status("INSERT 0 3") == 3

    def test_rows_from_status_delete(self):
        assert _rows_from_status("DELETE 2") == 2

    def test_dsn_falls_back_to_settings(self):
        from src.config import get_settings

        b = TimescaleBackend()  # no initialize — just constructor wiring
        assert b._dsn == get_settings().storage.timescale_dsn


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestInitializeAndClose:
    async def test_initialize_creates_tables(self, backend):
        health = await backend.health_check()
        assert "bars" in health
        assert health["bars"] == 0

    async def test_double_initialize_is_idempotent(self, backend):
        await backend.initialize()  # second call should no-op
        health = await backend.health_check()
        assert health["bars"] == 0

    async def test_require_pool_raises_before_init(self):
        b = TimescaleBackend(dsn="postgresql://unused@localhost/unused")
        with pytest.raises(RuntimeError, match="not initialized"):
            b._require_pool()

    async def test_method_raises_before_init(self):
        b = TimescaleBackend(dsn="postgresql://unused@localhost/unused")
        with pytest.raises(RuntimeError, match="not initialized"):
            await b.fetch_bars("BTC/USDT", "15m", since_ts=0)

    async def test_close_on_uninitialized_is_safe(self):
        b = TimescaleBackend(dsn="postgresql://unused@localhost/unused")
        await b.close()  # should not raise

    async def test_close_is_idempotent(self, test_dsn):
        b = TimescaleBackend(dsn=test_dsn)
        await b.initialize()
        await b.close()
        await b.close()  # second close — no-op

    async def test_open_timescale_storage_initializes_and_closes(self, test_dsn):
        async with open_timescale_storage(dsn=test_dsn) as storage:
            health = await storage.health_check()
            assert "bars" in health
        assert storage._pool is None

    async def test_bars_and_equity_are_hypertables(self, backend):
        async with backend._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT hypertable_name FROM timescaledb_information.hypertables"
            )
        names = {r["hypertable_name"] for r in rows}
        assert {"bars", "equity_curve"}.issubset(names)


# ---------------------------------------------------------------------------
# Schema migrations
# ---------------------------------------------------------------------------


class TestSchemaMigrations:
    async def test_fresh_db_sets_schema_version(self, backend):
        async with backend._pool.acquire() as conn:
            version = await conn.fetchval("SELECT MAX(version) FROM schema_version")
        assert version == _PG_SCHEMA_VERSION

    async def test_migration_is_idempotent(self, backend):
        await backend.initialize()  # up-to-date DB — must not raise
        async with backend._pool.acquire() as conn:
            version = await conn.fetchval("SELECT MAX(version) FROM schema_version")
        assert version == _PG_SCHEMA_VERSION

    async def test_partial_migration_reapplies_pending(self, backend, test_dsn):
        # Simulate a v1 DB: drop version markers 2..N (DDL is IF NOT EXISTS —
        # re-applying is safe), then re-open and check markers are restored.
        async with backend._pool.acquire() as conn:
            await conn.execute("DELETE FROM schema_version WHERE version > 1")
        fresh = TimescaleBackend(dsn=test_dsn)
        await fresh.initialize()
        try:
            async with fresh._pool.acquire() as conn:
                version = await conn.fetchval("SELECT MAX(version) FROM schema_version")
            assert version == _PG_SCHEMA_VERSION
        finally:
            await fresh.close()

    async def test_future_schema_version_raises(self, backend, test_dsn):
        async with backend._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO schema_version (version) VALUES ($1)",
                _PG_SCHEMA_VERSION + 99,
            )
        fresh = TimescaleBackend(dsn=test_dsn)
        try:
            with pytest.raises(RuntimeError, match="AHEAD of code"):
                await fresh.initialize()
        finally:
            await fresh.close()
            async with backend._pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM schema_version WHERE version > $1", _PG_SCHEMA_VERSION
                )


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------


class TestBars:
    async def test_upsert_bars_inserts_new(self, backend):
        inserted = await backend.upsert_bars([make_bar(ts=1000), make_bar(ts=2000)])
        assert inserted == 2

    async def test_upsert_bars_empty_list_returns_zero(self, backend):
        assert await backend.upsert_bars([]) == 0

    async def test_upsert_bars_duplicate_ignored(self, backend):
        bar = make_bar(ts=1000)
        await backend.upsert_bars([bar])
        assert await backend.upsert_bars([bar]) == 0

    async def test_upsert_bars_mixed_batch_counts_new_only(self, backend):
        await backend.upsert_bars([make_bar(ts=1000)])
        inserted = await backend.upsert_bars([make_bar(ts=1000), make_bar(ts=2000)])
        assert inserted == 1

    async def test_fetch_bars_returns_ordered(self, backend):
        await backend.upsert_bars([make_bar(ts=3000), make_bar(ts=1000), make_bar(ts=2000)])
        rows = await backend.fetch_bars("BTC/USDT", "15m", since_ts=0)
        assert [r.ts for r in rows] == [1000, 2000, 3000]

    async def test_fetch_bars_round_trips_values(self, backend):
        await backend.upsert_bars([make_bar(ts=1000, close=123.5)])
        (bar,) = await backend.fetch_bars("BTC/USDT", "15m", since_ts=0)
        assert bar.close == 123.5
        assert bar.quote_volume == 1000.0
        assert bar.taker_buy_vol == 5.0

    async def test_fetch_bars_respects_since_ts(self, backend):
        await backend.upsert_bars([make_bar(ts=1000), make_bar(ts=2000), make_bar(ts=3000)])
        rows = await backend.fetch_bars("BTC/USDT", "15m", since_ts=2000)
        assert [r.ts for r in rows] == [2000, 3000]

    async def test_fetch_bars_respects_limit(self, backend):
        await backend.upsert_bars([make_bar(ts=t) for t in range(10)])
        rows = await backend.fetch_bars("BTC/USDT", "15m", since_ts=0, limit=3)
        assert len(rows) == 3

    async def test_fetch_bars_empty_when_no_match(self, backend):
        assert await backend.fetch_bars("ETH/USDT", "15m", since_ts=0) == []

    async def test_bars_before_returns_ascending_up_to_ts(self, backend):
        await backend.upsert_bars(
            [make_bar(ts=t, close=float(t)) for t in (1000, 2000, 3000, 4000)]
        )
        rows = await backend.bars_before("BTC/USDT", "15m", ts=3000, limit=21)
        assert [r.ts for r in rows] == [1000, 2000, 3000]

    async def test_bars_before_respects_limit(self, backend):
        await backend.upsert_bars([make_bar(ts=t) for t in range(10)])
        rows = await backend.bars_before("BTC/USDT", "15m", ts=9, limit=3)
        assert [r.ts for r in rows] == [7, 8, 9]

    async def test_bars_before_empty_when_no_bar_at_or_before_ts(self, backend):
        await backend.upsert_bars([make_bar(ts=5000)])
        assert await backend.bars_before("BTC/USDT", "15m", ts=1000, limit=21) == []

    async def test_latest_bar_ts_returns_max(self, backend):
        await backend.upsert_bars([make_bar(ts=1000), make_bar(ts=5000)])
        assert await backend.latest_bar_ts("BTC/USDT", "15m") == 5000

    async def test_latest_bar_ts_none_when_empty(self, backend):
        assert await backend.latest_bar_ts("BTC/USDT", "15m") is None

    async def test_latest_close_returns_ts_and_close(self, backend):
        await backend.upsert_bars([make_bar(ts=1000, close=100.0), make_bar(ts=2000, close=105.0)])
        assert await backend.latest_close("BTC/USDT", "15m") == (2000, 105.0)

    async def test_latest_close_none_when_empty(self, backend):
        assert await backend.latest_close("BTC/USDT", "15m") is None

    async def test_bar_count(self, backend):
        await backend.upsert_bars([make_bar(ts=t) for t in range(5)])
        assert await backend.bar_count("BTC/USDT", "15m") == 5

    async def test_bar_count_zero_when_empty(self, backend):
        assert await backend.bar_count("BTC/USDT", "15m") == 0

    async def test_prune_old_bars_deletes_old(self, backend):
        await backend.upsert_bars([make_bar(ts=1)])  # epoch ms, ancient
        deleted = await backend.prune_old_bars("BTC/USDT", "15m", keep_days=30)
        assert deleted == 1

    async def test_prune_old_bars_keeps_recent(self, backend):
        recent_ts = int(time.time() * 1000)
        await backend.upsert_bars([make_bar(ts=recent_ts)])
        deleted = await backend.prune_old_bars("BTC/USDT", "15m", keep_days=30)
        assert deleted == 0
        assert await backend.bar_count("BTC/USDT", "15m") == 1


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------


class TestTrades:
    async def test_insert_trade(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1"))
        rows = await backend.fetch_trades()
        assert len(rows) == 1
        assert rows[0].id == "t1"
        assert rows[0].direction == 1

    async def test_insert_duplicate_trade_raises(self, backend):
        await backend.insert_trade(make_trade(trade_id="dup"))
        with pytest.raises(ValueError, match="already exists"):
            await backend.insert_trade(make_trade(trade_id="dup"))

    async def test_update_trade_exit_success(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1"))
        await backend.update_trade_exit(
            trade_id="t1",
            exit_price=110.0,
            exit_ts=2000,
            pnl_usd=10.0,
            pnl_pct=0.1,
            exit_reason="tp",
            fee_usd=0.05,
        )
        rows = await backend.fetch_trades()
        assert rows[0].exit_price == 110.0
        assert rows[0].pnl_usd == 10.0

    async def test_update_trade_exit_accumulates_fee(self, backend):
        trade = make_trade(trade_id="t1")
        trade.fee_usd = 0.1
        await backend.insert_trade(trade)
        await backend.update_trade_exit(
            trade_id="t1",
            exit_price=110.0,
            exit_ts=2000,
            pnl_usd=10.0,
            pnl_pct=0.1,
            exit_reason="tp",
            fee_usd=0.05,
        )
        rows = await backend.fetch_trades()
        assert rows[0].fee_usd == pytest.approx(0.15)

    async def test_update_trade_exit_nonexistent_raises(self, backend):
        with pytest.raises(ValueError, match="No open trade found"):
            await backend.update_trade_exit(
                trade_id="ghost",
                exit_price=110.0,
                exit_ts=2000,
                pnl_usd=10.0,
                pnl_pct=0.1,
                exit_reason="tp",
                fee_usd=0.05,
            )

    async def test_update_trade_exit_already_closed_raises(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1"))
        await backend.update_trade_exit(
            trade_id="t1",
            exit_price=110.0,
            exit_ts=2000,
            pnl_usd=10.0,
            pnl_pct=0.1,
            exit_reason="tp",
            fee_usd=0.05,
        )
        with pytest.raises(ValueError, match="already closed"):
            await backend.update_trade_exit(
                trade_id="t1",
                exit_price=120.0,
                exit_ts=3000,
                pnl_usd=20.0,
                pnl_pct=0.2,
                exit_reason="tp",
                fee_usd=0.05,
            )

    async def test_fetch_trades_open_only_excludes_closed(self, backend):
        await backend.insert_trade(make_trade(trade_id="open1"))
        await backend.insert_trade(make_trade(trade_id="closed1"))
        await backend.update_trade_exit(
            trade_id="closed1",
            exit_price=120.0,
            exit_ts=3000,
            pnl_usd=20.0,
            pnl_pct=0.2,
            exit_reason="tp",
            fee_usd=0.05,
        )
        rows = await backend.fetch_trades(open_only=True)
        assert [r.id for r in rows] == ["open1"]

    async def test_fetch_trades_open_only_combines_with_other_filters(self, backend):
        await backend.insert_trade(make_trade(trade_id="btc_open", symbol="BTC/USDT"))
        await backend.insert_trade(make_trade(trade_id="eth_open", symbol="ETH/USDT"))
        rows = await backend.fetch_trades(symbol="BTC/USDT", open_only=True)
        assert [r.id for r in rows] == ["btc_open"]

    async def test_fetch_trades_filter_by_symbol(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1", symbol="BTC/USDT"))
        await backend.insert_trade(make_trade(trade_id="t2", symbol="ETH/USDT"))
        rows = await backend.fetch_trades(symbol="BTC/USDT")
        assert len(rows) == 1
        assert rows[0].symbol == "BTC/USDT"

    async def test_fetch_trades_filter_by_trading_mode(self, backend):
        t1 = make_trade(trade_id="t1")
        t1.trading_mode = "live"
        await backend.insert_trade(t1)
        await backend.insert_trade(make_trade(trade_id="t2"))  # paper
        rows = await backend.fetch_trades(trading_mode="live")
        assert len(rows) == 1

    async def test_fetch_trades_filter_by_since_ts(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1", entry_ts=1000))
        await backend.insert_trade(make_trade(trade_id="t2", entry_ts=5000))
        rows = await backend.fetch_trades(since_ts=3000)
        assert len(rows) == 1
        assert rows[0].id == "t2"

    async def test_fetch_trades_all_filters_combined(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1", entry_ts=5000))
        await backend.insert_trade(make_trade(trade_id="t2", symbol="ETH/USDT", entry_ts=5000))
        rows = await backend.fetch_trades(symbol="BTC/USDT", trading_mode="paper", since_ts=1000)
        assert [r.id for r in rows] == ["t1"]

    async def test_fetch_trades_limit_and_offset(self, backend):
        for i in range(5):
            await backend.insert_trade(make_trade(trade_id=f"t{i}", entry_ts=1000 + i))
        rows = await backend.fetch_trades(limit=2, offset=1)
        assert len(rows) == 2

    async def test_fetch_trades_ordered_descending(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1", entry_ts=1000))
        await backend.insert_trade(make_trade(trade_id="t2", entry_ts=3000))
        await backend.insert_trade(make_trade(trade_id="t3", entry_ts=2000))
        rows = await backend.fetch_trades()
        assert [r.id for r in rows] == ["t2", "t3", "t1"]

    async def test_count_consecutive_losses_all_losses(self, backend):
        for i in range(3):
            await backend.insert_trade(
                make_trade(trade_id=f"t{i}", entry_ts=1000 + i, pnl_usd=-5.0)
            )
        assert await backend.count_consecutive_losses("BTC/USDT", "paper") == 3

    async def test_count_consecutive_losses_stops_at_win(self, backend):
        await backend.insert_trade(make_trade(trade_id="t1", entry_ts=1000, pnl_usd=-5.0))
        await backend.insert_trade(make_trade(trade_id="t2", entry_ts=2000, pnl_usd=10.0))
        await backend.insert_trade(make_trade(trade_id="t3", entry_ts=3000, pnl_usd=-5.0))
        assert await backend.count_consecutive_losses("BTC/USDT", "paper") == 1

    async def test_count_consecutive_losses_no_trades(self, backend):
        assert await backend.count_consecutive_losses("BTC/USDT", "paper") == 0

    async def test_daily_pnl_sums_todays_trades(self, backend):
        now_ms = int(time.time() * 1000)
        await backend.insert_trade(
            make_trade(trade_id="t1", entry_ts=now_ms, pnl_usd=15.0, exit_ts=now_ms)
        )
        assert await backend.daily_pnl("BTC/USDT", "paper") == pytest.approx(15.0)

    async def test_daily_pnl_zero_when_no_trades(self, backend):
        assert await backend.daily_pnl("BTC/USDT", "paper") == 0.0

    async def test_daily_pnl_excludes_old_trades(self, backend):
        await backend.insert_trade(
            make_trade(trade_id="t1", entry_ts=1000, pnl_usd=15.0, exit_ts=1000)
        )
        assert await backend.daily_pnl("BTC/USDT", "paper") == 0.0


# ---------------------------------------------------------------------------
# Regime snapshots
# ---------------------------------------------------------------------------


class TestRegimeSnapshots:
    async def test_upsert_and_fetch_latest_regime(self, backend):
        await backend.upsert_regime_snapshot(make_regime(ts=1000, state=0))
        await backend.upsert_regime_snapshot(make_regime(ts=2000, state=1))
        latest = await backend.latest_regime("BTC/USDT", "15m")
        assert latest.ts == 2000
        assert latest.regime_state == 1

    async def test_latest_regime_none_when_empty(self, backend):
        assert await backend.latest_regime("BTC/USDT", "15m") is None

    async def test_upsert_regime_replaces_same_ts(self, backend):
        await backend.upsert_regime_snapshot(make_regime(ts=1000, state=0))
        await backend.upsert_regime_snapshot(make_regime(ts=1000, state=2))
        latest = await backend.latest_regime("BTC/USDT", "15m")
        assert latest.regime_state == 2

    async def test_regime_snapshot_before(self, backend):
        await backend.upsert_regime_snapshot(make_regime(ts=1000, state=0))
        await backend.upsert_regime_snapshot(make_regime(ts=2000, state=1))
        await backend.upsert_regime_snapshot(make_regime(ts=3000, state=2))

        snap = await backend.regime_snapshot_before("BTC/USDT", "15m", 2500)
        assert snap.ts == 2000
        assert snap.regime_state == 1

    async def test_regime_snapshot_before_none_when_no_earlier(self, backend):
        await backend.upsert_regime_snapshot(make_regime(ts=5000))
        assert await backend.regime_snapshot_before("BTC/USDT", "15m", 1000) is None


# ---------------------------------------------------------------------------
# Missed trades (UI-001)
# ---------------------------------------------------------------------------


class TestMissedTrades:
    async def test_insert_and_fetch_missed_trade(self, backend):
        from src.data.storage import MissedTradeRecord

        await backend.insert_missed_trade(
            MissedTradeRecord(
                id="m1",
                symbol="BTC/USDT",
                timeframe="15m",
                direction=1,
                reason="rejected",
                kelly_fraction=0.05,
                meta_label_prob=0.6,
                raw_signal=0.55,
                regime_at_entry=1,
                notional_usd=500.0,
                ts=2000,
            )
        )
        fetched = await backend.fetch_missed_trades(symbol="BTC/USDT")
        assert len(fetched) == 1
        assert fetched[0].id == "m1"
        assert fetched[0].reason == "rejected"

    async def test_fetch_missed_trades_filters_by_symbol(self, backend):
        from src.data.storage import MissedTradeRecord

        for i, sym in enumerate(["BTC/USDT", "ETH/USDT"]):
            await backend.insert_missed_trade(
                MissedTradeRecord(
                    id=f"m{i}",
                    symbol=sym,
                    timeframe="15m",
                    direction=0,
                    reason="skipped",
                    kelly_fraction=0.01,
                    meta_label_prob=0.5,
                    raw_signal=None,
                    regime_at_entry=0,
                    notional_usd=10.0,
                    ts=1000 + i,
                )
            )
        btc_only = await backend.fetch_missed_trades(symbol="BTC/USDT")
        assert len(btc_only) == 1
        assert btc_only[0].symbol == "BTC/USDT"


# ---------------------------------------------------------------------------
# Model metrics
# ---------------------------------------------------------------------------


class TestModelMetrics:
    async def test_insert_and_fetch_latest_metrics(self, backend):
        await backend.insert_model_metrics(make_metrics(version="v1"))
        latest = await backend.latest_model_metrics("direction", "15m")
        assert latest.version == "v1"
        assert latest.live_gate_pass is True

    async def test_insert_replaces_same_version(self, backend):
        await backend.insert_model_metrics(make_metrics(version="v1", gate_pass=True))
        await backend.insert_model_metrics(make_metrics(version="v1", gate_pass=False))
        latest = await backend.latest_model_metrics("direction", "15m")
        assert latest.live_gate_pass is False

    async def test_latest_model_metrics_none_when_empty(self, backend):
        assert await backend.latest_model_metrics("direction", "15m") is None

    async def test_live_gate_passes_true_when_both_pass(self, backend):
        await backend.insert_model_metrics(make_metrics(model_name="direction", gate_pass=True))
        await backend.insert_model_metrics(make_metrics(model_name="meta_label", gate_pass=True))
        assert await backend.live_gate_passes("15m") is True

    async def test_live_gate_passes_false_when_one_fails(self, backend):
        await backend.insert_model_metrics(make_metrics(model_name="direction", gate_pass=True))
        await backend.insert_model_metrics(make_metrics(model_name="meta_label", gate_pass=False))
        assert await backend.live_gate_passes("15m") is False

    async def test_live_gate_passes_false_when_missing(self, backend):
        await backend.insert_model_metrics(make_metrics(model_name="direction", gate_pass=True))
        assert await backend.live_gate_passes("15m") is False

    async def test_live_gate_passes_false_when_no_data(self, backend):
        assert await backend.live_gate_passes("15m") is False


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------


class TestEquityCurve:
    async def test_insert_and_fetch_equity_curve(self, backend):
        await backend.insert_equity(make_equity(ts=1000, equity=10000.0))
        await backend.insert_equity(make_equity(ts=2000, equity=10500.0))
        curve = await backend.fetch_equity_curve("paper")
        assert [e.ts for e in curve] == [1000, 2000]

    async def test_fetch_equity_curve_since_ts(self, backend):
        await backend.insert_equity(make_equity(ts=1000))
        await backend.insert_equity(make_equity(ts=2000))
        curve = await backend.fetch_equity_curve("paper", since_ts=1500)
        assert len(curve) == 1
        assert curve[0].ts == 2000

    async def test_fetch_equity_curve_limit(self, backend):
        for t in range(5):
            await backend.insert_equity(make_equity(ts=t))
        curve = await backend.fetch_equity_curve("paper", limit=2)
        assert len(curve) == 2

    async def test_latest_equity_returns_most_recent(self, backend):
        await backend.insert_equity(make_equity(ts=1000, equity=10000.0))
        await backend.insert_equity(make_equity(ts=2000, equity=10500.0))
        latest = await backend.latest_equity("paper")
        assert latest.equity_usd == 10500.0

    async def test_latest_equity_none_when_empty(self, backend):
        assert await backend.latest_equity("paper") is None

    async def test_earliest_equity_ts(self, backend):
        await backend.insert_equity(make_equity(ts=3000))
        await backend.insert_equity(make_equity(ts=1000))
        assert await backend.earliest_equity_ts("paper") == 1000

    async def test_earliest_equity_ts_none_when_empty(self, backend):
        assert await backend.earliest_equity_ts("paper") is None

    async def test_insert_equity_upserts_same_ts(self, backend):
        await backend.insert_equity(make_equity(ts=1000, equity=10000.0))
        await backend.insert_equity(make_equity(ts=1000, equity=20000.0))
        latest = await backend.latest_equity("paper")
        assert latest.equity_usd == 20000.0

    async def test_same_ts_different_mode_both_kept(self, backend):
        # SCAN3-005 parity: paper and live snapshots on the same millisecond.
        await backend.insert_equity(make_equity(ts=1000, trading_mode="paper"))
        await backend.insert_equity(make_equity(ts=1000, trading_mode="live"))
        assert len(await backend.fetch_equity_curve("paper")) == 1
        assert len(await backend.fetch_equity_curve("live")) == 1


# ---------------------------------------------------------------------------
# Symbol validation
# ---------------------------------------------------------------------------


class TestValidateSymbol:
    async def test_valid_symbol_exists(self, backend):
        await backend.upsert_bars([make_bar(symbol="BTC/USDT")])
        await backend.validate_symbol("BTC/USDT")  # should not raise

    async def test_invalid_format_raises(self, backend):
        with pytest.raises(ValueError, match="Invalid symbol format"):
            await backend.validate_symbol("not-a-symbol")

    async def test_lowercase_format_raises(self, backend):
        with pytest.raises(ValueError, match="Invalid symbol format"):
            await backend.validate_symbol("btc/usdt")

    async def test_valid_format_but_unknown_raises(self, backend):
        with pytest.raises(ValueError, match="Unknown symbol"):
            await backend.validate_symbol("XYZ/USDT")

    async def test_missing_slash_raises(self, backend):
        with pytest.raises(ValueError, match="Invalid symbol format"):
            await backend.validate_symbol("BTCUSDT")


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class TestAuditLog:
    async def test_insert_audit_event_basic(self, backend):
        await backend.insert_audit_event(event_type="mode_change", operator="admin")
        health = await backend.health_check()
        assert health["audit_log"] == 1

    async def test_insert_audit_event_with_details(self, backend):
        await backend.insert_audit_event(
            event_type="mode_change",
            operator="admin",
            details={"from": "paper", "to": "live"},
        )
        health = await backend.health_check()
        assert health["audit_log"] == 1

    async def test_insert_audit_event_none_details_defaults_empty(self, backend):
        await backend.insert_audit_event(event_type="startup", operator="system", details=None)
        health = await backend.health_check()
        assert health["audit_log"] == 1


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    async def test_health_check_all_tables_present(self, backend):
        health = await backend.health_check()
        expected_tables = {
            "bars",
            "trades",
            "regime_snapshots",
            "model_metrics",
            "equity_curve",
            "audit_log",
        }
        assert expected_tables.issubset(set(health.keys()))

    async def test_health_check_reflects_inserted_data(self, backend):
        await backend.upsert_bars([make_bar(ts=1000)])
        await backend.insert_trade(make_trade(trade_id="t1"))
        health = await backend.health_check()
        assert health["bars"] == 1
        assert health["trades"] == 1

    async def test_health_check_rejects_unsafe_table_name(self, backend, monkeypatch):
        monkeypatch.setattr("src.data.timescale_storage._ALLOWED_TABLES", frozenset({"bad table"}))
        with pytest.raises(RuntimeError, match="unsafe characters"):
            await backend.health_check()


# ---------------------------------------------------------------------------
# Intelligence features history (GAP-015 parity)
# ---------------------------------------------------------------------------


class TestIntelligenceFeatures:
    async def test_store_and_fetch_features(self, backend):
        await backend.store_intelligence_features(
            symbol="BTCUSDT",
            timeframe="1h",
            bar_ts=1000,
            features=make_features(),
            confidence=0.9,
        )
        df = await backend.fetch_intelligence_features("BTCUSDT", "1h")
        assert len(df) == 1
        assert df.index[0] == 1000
        assert df.loc[1000, "intelligence_exchange_netflow_7d_zscore"] == pytest.approx(1.5)
        assert df.loc[1000, "intelligence_mvrv_z_score"] == pytest.approx(2.2)
        assert df.loc[1000, "intelligence_confidence"] == pytest.approx(0.9)

    async def test_fetch_features_empty_returns_empty_df(self, backend):
        df = await backend.fetch_intelligence_features("BTCUSDT", "1h")
        assert df.empty

    async def test_store_features_upserts_same_bar(self, backend):
        await backend.store_intelligence_features(
            "BTCUSDT", "1h", 1000, make_features(), confidence=0.5
        )
        await backend.store_intelligence_features(
            "BTCUSDT",
            "1h",
            1000,
            make_features(intelligence_sopr=1.5),
            confidence=0.8,
        )
        df = await backend.fetch_intelligence_features("BTCUSDT", "1h")
        assert len(df) == 1
        assert df.loc[1000, "intelligence_sopr"] == pytest.approx(1.5)
        assert df.loc[1000, "intelligence_confidence"] == pytest.approx(0.8)

    async def test_missing_feature_keys_stored_as_null(self, backend):
        await backend.store_intelligence_features(
            "BTCUSDT", "1h", 1000, {"intelligence_sopr": 1.1}, confidence=0.5
        )
        df = await backend.fetch_intelligence_features("BTCUSDT", "1h")
        assert df.loc[1000, "intelligence_whale_buy_sell_ratio"] is None or (
            df.loc[1000, "intelligence_whale_buy_sell_ratio"]
            != df.loc[1000, "intelligence_whale_buy_sell_ratio"]  # NaN
        )

    async def test_fetch_features_respects_since_ts(self, backend):
        for ts in (1000, 2000, 3000):
            await backend.store_intelligence_features(
                "BTCUSDT", "1h", ts, make_features(), confidence=0.5
            )
        df = await backend.fetch_intelligence_features("BTCUSDT", "1h", since_ts=2000)
        assert list(df.index) == [2000, 3000]

    async def test_coverage_empty(self, backend):
        cov = await backend.intelligence_feature_coverage("BTCUSDT", "1h")
        assert cov == {"total_rows": 0, "coverage": {}}

    async def test_coverage_fractions(self, backend):
        await backend.store_intelligence_features(
            "BTCUSDT", "1h", 1000, make_features(), confidence=0.5
        )
        await backend.store_intelligence_features(
            "BTCUSDT", "1h", 2000, {"intelligence_sopr": 1.0}, confidence=0.5
        )
        cov = await backend.intelligence_feature_coverage("BTCUSDT", "1h")
        assert cov["total_rows"] == 2
        assert cov["coverage"]["intelligence_sopr"] == pytest.approx(1.0)
        assert cov["coverage"]["intelligence_whale_buy_sell_ratio"] == pytest.approx(0.5)
        assert cov["coverage"]["intelligence_miner_netflow_signal"] == pytest.approx(0.0)
