"""
Async SQLite storage layer — aiosqlite, WAL mode, typed queries.

Schema owns five tables:
  bars            — OHLCV + volume per symbol/timeframe
  trades          — paper and live trade records with full audit trail
  regime_snapshots — HMM state at every bar (Hamilton 1989)
  model_metrics   — CPCV OOS metrics per model version (AFML Ch.7)
  equity_curve    — timestamped equity snapshots for drawdown tracking

Authority sources:
  - aiosqlite docs (https://aiosqlite.omnilib.dev/en/latest/)
  - SQLite WAL mode (https://www.sqlite.org/wal.html)
  - López de Prado (2018) AFML — trade audit requirements
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Union


if TYPE_CHECKING:
    import pandas as pd

    from src.data.timescale_storage import TimescaleBackend

import aiosqlite
import structlog

from src.config import get_settings


# GAP-006: either storage backend — identical public interface, callers are
# agnostic. Forward-ref strings keep the asyncpg-backed module a deferred
# import (only loaded when STORAGE_BACKEND=timescale).
AnyStorageBackend = Union["StorageBackend", "TimescaleBackend"]


# ---------------------------------------------------------------------------
# Schema migrations — Gap-012
# ---------------------------------------------------------------------------
# Every schema change MUST be expressed as an entry in _MIGRATIONS.
# Each entry is (version: int, description: str, sql: str).
# Migrations are applied in order at startup if PRAGMA user_version is behind.
# Current schema version: _SCHEMA_VERSION (the length of _MIGRATIONS list).
#
# Rules:
#   - Only forward migrations (no rollback support — backup before running).
#   - Pure DDL: ALTER TABLE, CREATE TABLE, CREATE INDEX, CREATE VIEW only.
#   - Never mutate existing rows here; do that in a separate data-migration script.
#   - After adding a migration, bump the version by appending to this list.
#     The version is implicit: version N is _MIGRATIONS[N-1].
#
# Example of a future migration:
#   (
#       3,
#       "gap-004: add order_fsm_state column to trades for FSM tracking",
#       "ALTER TABLE trades ADD COLUMN order_fsm_state TEXT NOT NULL DEFAULT 'CLOSED';"
#   ),
_MIGRATIONS: Final[list[tuple[int, str, str]]] = [
    # Version 1 — initial schema (tables created by _DDL above; no ALTER needed,
    # but we register version 1 so any DB initialised after Gap-012 has a
    # user_version of 1 set, and older DBs (user_version=0) are detected and
    # brought up by the migration runner).
    (
        1,
        "initial schema: bars, trades, regime_snapshots, model_metrics, equity_curve, audit_log",
        "",  # Empty SQL: tables already exist from _DDL; we just set the version.
    ),
    # Version 2 — add spread_bps column to trades (GAP-008/TASK-009 follow-on)
    (
        2,
        "add spread_bps to trades for slippage audit trail",
        "ALTER TABLE trades ADD COLUMN spread_bps REAL;",
    ),
    # Version 3 — GAP-015: intelligence features history for model training.
    # Stores one row per (symbol, timeframe, bar_ts) with all 18 intelligence
    # feature values and per-row confidence. bar_ts matches bars.ts (Unix ms)
    # so the backfill script can JOIN/align by timestamp.
    # NULLs permitted: NULL = provider had no data for that bar.
    # Trainer coverage check must reject columns with >threshold NULL rate.
    (
        3,
        "gap-015: add intelligence_features_history table for backfill pipeline",
        """CREATE TABLE IF NOT EXISTS intelligence_features_history (
    symbol      TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    bar_ts      INTEGER NOT NULL,
    fetched_at  INTEGER NOT NULL,
    exchange_netflow_7d_zscore      REAL,
    whale_buy_sell_ratio            REAL,
    exchange_reserve_ratio          REAL,
    miner_netflow_signal            REAL,
    staking_unlock_risk             REAL,
    entity_exchange_imbalance       REAL,
    binance_funding_rate_pct        REAL,
    liquidation_pressure_24h_zscore REAL,
    futures_oi_change_pct           REAL,
    liquidation_cascade_risk_usd    REAL,
    btc_dominance_regime            REAL,
    stablecoin_reserve_ratio        REAL,
    network_activity_score          REAL,
    exchange_stress_score           REAL,
    cross_exchange_basis_spread_bps REAL,
    confidence  REAL NOT NULL DEFAULT 0.0,
    source      TEXT NOT NULL DEFAULT 'backfill',
    PRIMARY KEY (symbol, timeframe, bar_ts)
);
CREATE INDEX IF NOT EXISTS idx_intel_hist_ts
    ON intelligence_features_history (symbol, timeframe, bar_ts ASC);""",
    ),
    # v4 — OCI-012: add defi_tvl_7d_change_pct, mvrv_z_score, sopr columns
    (
        4,
        "oci-012: add defi_tvl/mvrv_z_score/sopr to intelligence_features_history",
        """ALTER TABLE intelligence_features_history ADD COLUMN defi_tvl_7d_change_pct REAL;
ALTER TABLE intelligence_features_history ADD COLUMN mvrv_z_score REAL;
ALTER TABLE intelligence_features_history ADD COLUMN sopr REAL;""",
    ),
    # v5 — UI-001: missed_trades table. Records every signal the engine
    # judged tradeable but that never resulted in an open position (gate
    # rejection, approval denial/timeout, drift block). Distinct from
    # `trades`, which only ever holds executed positions.
    (
        5,
        "ui-001: add missed_trades table for the dashboard's Missed Trades tab",
        """CREATE TABLE IF NOT EXISTS missed_trades (
    id              TEXT    PRIMARY KEY,
    symbol          TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,
    direction       INTEGER NOT NULL,
    reason          TEXT    NOT NULL,
    kelly_fraction  REAL    NOT NULL,
    meta_label_prob REAL    NOT NULL,
    raw_signal      REAL,
    regime_at_entry INTEGER NOT NULL,
    notional_usd    REAL    NOT NULL,
    ts              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_missed_trades_ts
    ON missed_trades(ts DESC);""",
    ),
    # v6 — add changepoint_probability and agreement_score to regime_snapshots.
    # Enables monitoring of HMM/changepoint ensemble disagreement over time
    # (regime_agreement_score wired into cognitive engine risk, GAP-regime-ensemble).
    (
        6,
        "regime-ensemble: add changepoint_probability and agreement_score to regime_snapshots",
        """ALTER TABLE regime_snapshots ADD COLUMN changepoint_probability REAL NOT NULL DEFAULT 0.0;
ALTER TABLE regime_snapshots ADD COLUMN agreement_score REAL NOT NULL DEFAULT 1.0;""",
    ),
]

_SCHEMA_VERSION: Final[int] = len(_MIGRATIONS)  # = 6


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# DDL — all tables created once on first connection
# ---------------------------------------------------------------------------

_DDL: Final[str] = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
-- Gap-006: synchronous=NORMAL is safe with WAL (data durability is preserved;
-- only the WAL header may be lost on OS crash, not committed data). FULL
-- adds an extra fsync per commit; NORMAL gives 2-4x write throughput benefit
-- with no practical durability loss on modern kernels with battery-backed SSD.
-- Ref: https://www.sqlite.org/pragma.html#pragma_synchronous
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
-- Gap-006: 64MB page cache (~8192 pages x 8KB). Reduces read I/O for
-- the bars + trades tables during signal generation ticks.
PRAGMA cache_size=-65536;
-- Gap-006: 256MB mmap for sequential scans (bars fetch).
PRAGMA mmap_size=268435456;
PRAGMA temp_store=MEMORY;

CREATE TABLE IF NOT EXISTS bars (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,
    ts              INTEGER NOT NULL,   -- Unix ms, UTC
    open            REAL    NOT NULL,
    high            REAL    NOT NULL,
    low             REAL    NOT NULL,
    close           REAL    NOT NULL,
    volume          REAL    NOT NULL,
    quote_volume    REAL    NOT NULL DEFAULT 0.0,
    taker_buy_vol   REAL    NOT NULL DEFAULT 0.0,
    inserted_at     INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),
    UNIQUE(symbol, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_sym_tf_ts
    ON bars(symbol, timeframe, ts DESC);

CREATE TABLE IF NOT EXISTS trades (
    id              TEXT    PRIMARY KEY,  -- UUID
    symbol          TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,
    trading_mode    TEXT    NOT NULL,     -- paper | live
    execution_mode  TEXT    NOT NULL,     -- automatic | restricted | manual
    direction       INTEGER NOT NULL,     -- 1=long, 0=short
    entry_price     REAL    NOT NULL,
    exit_price      REAL,
    quantity        REAL    NOT NULL,
    notional_usd    REAL    NOT NULL,
    entry_ts        INTEGER NOT NULL,
    exit_ts         INTEGER,
    pnl_usd         REAL,
    pnl_pct         REAL,
    fee_usd         REAL    NOT NULL DEFAULT 0.0,
    kelly_fraction  REAL    NOT NULL,
    regime_at_entry INTEGER NOT NULL,     -- 0=ranging,1=trending,2=volatile
    meta_label_prob REAL    NOT NULL,     -- meta-label gate probability
    exit_reason     TEXT,                 -- profit_target|stop_loss|time_exit|manual
    approved_by     TEXT,                 -- operator id or 'auto'
    raw_signal      REAL,                 -- XGBoost primary probability
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
);
CREATE INDEX IF NOT EXISTS idx_trades_sym_ts
    ON trades(symbol, entry_ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_mode
    ON trades(trading_mode, entry_ts DESC);

CREATE TABLE IF NOT EXISTS regime_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,
    ts              INTEGER NOT NULL,
    regime_state    INTEGER NOT NULL,     -- 0|1|2
    prob_ranging    REAL    NOT NULL,
    prob_trending   REAL    NOT NULL,
    prob_volatile   REAL    NOT NULL,
    UNIQUE(symbol, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_regime_sym_tf_ts
    ON regime_snapshots(symbol, timeframe, ts DESC);

CREATE TABLE IF NOT EXISTS model_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name      TEXT    NOT NULL,     -- 'direction' | 'meta_label'
    timeframe       TEXT    NOT NULL,
    version         TEXT    NOT NULL,     -- ISO timestamp of training run
    oos_sharpe      REAL    NOT NULL,
    max_drawdown    REAL    NOT NULL,
    n_trades        INTEGER NOT NULL,
    accuracy        REAL    NOT NULL,
    precision_score REAL    NOT NULL,
    recall_score    REAL    NOT NULL,
    f1_score        REAL    NOT NULL,
    live_gate_pass  INTEGER NOT NULL DEFAULT 0,  -- 1 if all thresholds met
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),
    UNIQUE(model_name, timeframe, version)
);
CREATE INDEX IF NOT EXISTS idx_model_metrics_name_tf
    ON model_metrics(model_name, timeframe, created_at DESC);

CREATE TABLE IF NOT EXISTS equity_curve (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    trading_mode    TEXT    NOT NULL,
    equity_usd      REAL    NOT NULL,
    cash_usd        REAL    NOT NULL,
    unrealized_pnl  REAL    NOT NULL DEFAULT 0.0,
    daily_pnl_usd   REAL    NOT NULL DEFAULT 0.0,
    daily_pnl_pct   REAL    NOT NULL DEFAULT 0.0,
    peak_equity_usd REAL    NOT NULL,
    drawdown_pct    REAL    NOT NULL DEFAULT 0.0,
    -- SCAN3-005: changed from UNIQUE(ts) to UNIQUE(ts, trading_mode) so paper and
    -- live equity snapshots on the same millisecond don't silently drop each other.
    UNIQUE(ts, trading_mode)
);
CREATE INDEX IF NOT EXISTS idx_equity_ts
    ON equity_curve(ts DESC);
-- SCAN3-008 (bars ASC range scan): separate ascending index for training queries
CREATE INDEX IF NOT EXISTS idx_bars_sym_tf_ts_asc
    ON bars(symbol, timeframe, ts ASC);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000),
    event_type      TEXT    NOT NULL,  -- e.g. 'execution_mode_change'
    operator        TEXT    NOT NULL,
    details         TEXT    NOT NULL DEFAULT '{}'  -- JSON blob
);
CREATE INDEX IF NOT EXISTS idx_audit_ts
    ON audit_log(ts DESC);
"""

# ---------------------------------------------------------------------------
# Allowed table names for health_check — prevents f-string injection
# ---------------------------------------------------------------------------

_ALLOWED_TABLES: Final[frozenset[str]] = frozenset(
    {"bars", "trades", "regime_snapshots", "model_metrics", "equity_curve", "audit_log"}
)

# ---------------------------------------------------------------------------
# Allowed trading pair pattern for symbol validation
# ---------------------------------------------------------------------------

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}/[A-Z0-9]{2,10}$")

# ---------------------------------------------------------------------------
# Record dataclasses — typed transport objects, no ORM overhead
# ---------------------------------------------------------------------------


class BarRecord:
    """Single OHLCV bar as returned by storage queries."""

    __slots__ = (
        "close",
        "high",
        "low",
        "open",
        "quote_volume",
        "symbol",
        "taker_buy_vol",
        "timeframe",
        "ts",
        "volume",
    )

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        ts: int,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        quote_volume: float = 0.0,
        taker_buy_vol: float = 0.0,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.ts = ts
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.quote_volume = quote_volume
        self.taker_buy_vol = taker_buy_vol


class TradeRecord:
    """Full trade audit record."""

    __slots__ = (
        "approved_by",
        "direction",
        "entry_price",
        "entry_ts",
        "execution_mode",
        "exit_price",
        "exit_reason",
        "exit_ts",
        "fee_usd",
        "id",
        "kelly_fraction",
        "meta_label_prob",
        "notional_usd",
        "pnl_pct",
        "pnl_usd",
        "quantity",
        "raw_signal",
        "regime_at_entry",
        "symbol",
        "timeframe",
        "trading_mode",
    )

    def __init__(
        self,
        id: str,
        symbol: str,
        timeframe: str,
        trading_mode: str,
        execution_mode: str,
        direction: int,
        entry_price: float,
        exit_price: float | None,
        quantity: float,
        notional_usd: float,
        entry_ts: int,
        exit_ts: int | None,
        pnl_usd: float | None,
        pnl_pct: float | None,
        fee_usd: float,
        kelly_fraction: float,
        regime_at_entry: int,
        meta_label_prob: float,
        exit_reason: str | None,
        approved_by: str | None,
        raw_signal: float | None,
    ) -> None:
        self.id = id
        self.symbol = symbol
        self.timeframe = timeframe
        self.trading_mode = trading_mode
        self.execution_mode = execution_mode
        self.direction = direction
        self.entry_price = entry_price
        self.exit_price = exit_price
        self.quantity = quantity
        self.notional_usd = notional_usd
        self.entry_ts = entry_ts
        self.exit_ts = exit_ts
        self.pnl_usd = pnl_usd
        self.pnl_pct = pnl_pct
        self.fee_usd = fee_usd
        self.kelly_fraction = kelly_fraction
        self.regime_at_entry = regime_at_entry
        self.meta_label_prob = meta_label_prob
        self.exit_reason = exit_reason
        self.approved_by = approved_by
        self.raw_signal = raw_signal


class MissedTradeRecord:
    """A signal the engine judged tradeable that never became an open
    position — gate rejection, approval denial/timeout, or a drift block.
    See UI-001 / missed_trades migration (v5)."""

    __slots__ = (
        "direction",
        "id",
        "kelly_fraction",
        "meta_label_prob",
        "notional_usd",
        "raw_signal",
        "reason",
        "regime_at_entry",
        "symbol",
        "timeframe",
        "ts",
    )

    def __init__(
        self,
        id: str,
        symbol: str,
        timeframe: str,
        direction: int,
        reason: str,
        kelly_fraction: float,
        meta_label_prob: float,
        raw_signal: float | None,
        regime_at_entry: int,
        notional_usd: float,
        ts: int,
    ) -> None:
        self.id = id
        self.symbol = symbol
        self.timeframe = timeframe
        self.direction = direction
        self.reason = reason
        self.kelly_fraction = kelly_fraction
        self.meta_label_prob = meta_label_prob
        self.raw_signal = raw_signal
        self.regime_at_entry = regime_at_entry
        self.notional_usd = notional_usd
        self.ts = ts


class RegimeSnapshotRecord:
    """HMM regime state at a single bar, including ensemble agreement metrics."""

    __slots__ = (
        "agreement_score",
        "changepoint_probability",
        "prob_ranging",
        "prob_trending",
        "prob_volatile",
        "regime_state",
        "symbol",
        "timeframe",
        "ts",
    )

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        ts: int,
        regime_state: int,
        prob_ranging: float,
        prob_trending: float,
        prob_volatile: float,
        changepoint_probability: float = 0.0,
        agreement_score: float = 1.0,
    ) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self.ts = ts
        self.regime_state = regime_state
        self.prob_ranging = prob_ranging
        self.prob_trending = prob_trending
        self.prob_volatile = prob_volatile
        self.changepoint_probability = changepoint_probability
        self.agreement_score = agreement_score


class ModelMetricsRecord:
    """CPCV OOS metrics snapshot for a trained model version."""

    __slots__ = (
        "accuracy",
        "f1_score",
        "live_gate_pass",
        "max_drawdown",
        "model_name",
        "n_trades",
        "oos_sharpe",
        "precision_score",
        "recall_score",
        "timeframe",
        "version",
    )

    def __init__(
        self,
        model_name: str,
        timeframe: str,
        version: str,
        oos_sharpe: float,
        max_drawdown: float,
        n_trades: int,
        accuracy: float,
        precision_score: float,
        recall_score: float,
        f1_score: float,
        live_gate_pass: bool,
    ) -> None:
        self.model_name = model_name
        self.timeframe = timeframe
        self.version = version
        self.oos_sharpe = oos_sharpe
        self.max_drawdown = max_drawdown
        self.n_trades = n_trades
        self.accuracy = accuracy
        self.precision_score = precision_score
        self.recall_score = recall_score
        self.f1_score = f1_score
        self.live_gate_pass = live_gate_pass


class EquityRecord:
    """Point-in-time equity snapshot."""

    __slots__ = (
        "cash_usd",
        "daily_pnl_pct",
        "daily_pnl_usd",
        "drawdown_pct",
        "equity_usd",
        "peak_equity_usd",
        "trading_mode",
        "ts",
        "unrealized_pnl",
    )

    def __init__(
        self,
        ts: int,
        trading_mode: str,
        equity_usd: float,
        cash_usd: float,
        unrealized_pnl: float,
        daily_pnl_usd: float,
        daily_pnl_pct: float,
        peak_equity_usd: float,
        drawdown_pct: float,
    ) -> None:
        self.ts = ts
        self.trading_mode = trading_mode
        self.equity_usd = equity_usd
        self.cash_usd = cash_usd
        self.unrealized_pnl = unrealized_pnl
        self.daily_pnl_usd = daily_pnl_usd
        self.daily_pnl_pct = daily_pnl_pct
        self.peak_equity_usd = peak_equity_usd
        self.drawdown_pct = drawdown_pct


# ---------------------------------------------------------------------------
# StorageBackend — single async connection pool
# ---------------------------------------------------------------------------


class StorageBackend:
    """
    Async SQLite storage backend.

    Lifecycle:
        backend = StorageBackend()
        await backend.initialize()
        ...
        await backend.close()

    Or use as an async context manager via open_storage().
    """

    def __init__(self, db_path: str | None = None) -> None:
        cfg = get_settings().storage
        self._db_path: str = db_path or str(cfg.db_path)
        self._conn: aiosqlite.Connection | None = None
        # VF-011: asyncio.Lock() created at __init__ time would raise
        # DeprecationWarning (Python 3.10+) when no event loop is running.
        # Use the same double-checked locking pattern as RuntimeConfig (VF-004).
        self._lock_init_guard: threading.Lock = threading.Lock()
        self._lock: asyncio.Lock | None = None
        self._log = log.bind(component="storage", db=self._db_path)

    def _get_lock(self) -> asyncio.Lock:
        """Return (lazily-created) asyncio.Lock — thread-safe one-time init."""
        if self._lock is not None:
            return self._lock
        with self._lock_init_guard:
            if self._lock is None:
                self._lock = asyncio.Lock()
        assert self._lock is not None
        return self._lock

    async def initialize(self) -> None:
        """Open WAL-mode connection, create required directories, and apply DDL."""
        # Create storage directories here — not in the Pydantic validator —
        # so that mkdir() only runs once at startup, not on every Settings instantiation.
        cfg = get_settings().storage
        for p in (cfg.db_path.parent, cfg.model_dir, cfg.log_dir):
            p.mkdir(parents=True, exist_ok=True)

        async with self._get_lock():
            if self._conn is not None:
                return
            self._conn = await aiosqlite.connect(self._db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.executescript(_DDL)
            await self._conn.commit()
            await self._run_migrations()
            self._log.info("storage.initialized", schema_version=_SCHEMA_VERSION)

    async def _run_migrations(self) -> None:
        """Apply any pending schema migrations via PRAGMA user_version."""
        conn = self._conn
        assert conn is not None

        row = await conn.execute("PRAGMA user_version")
        fetched = await row.fetchone()
        assert fetched is not None, "PRAGMA user_version returned no row"
        current: int = fetched[0]

        if current == _SCHEMA_VERSION:
            return  # Already up to date.

        if current > _SCHEMA_VERSION:
            raise RuntimeError(
                f"DB schema version {current} is AHEAD of code version "
                f"{_SCHEMA_VERSION}. Downgrade not supported — restore from backup."
            )

        self._log.info(
            "storage.migration.start",
            from_version=current,
            to_version=_SCHEMA_VERSION,
        )
        for version, description, sql in _MIGRATIONS:
            if version <= current:
                continue  # Already applied.
            self._log.info(
                "storage.migration.apply",
                version=version,
                description=description,
            )
            if sql.strip():
                await conn.executescript(sql)
            # Bump user_version inside the same transaction context.
            # user_version PRAGMA cannot be set inside executescript (it's DDL-level),
            # so we use a raw execute. aiosqlite auto-commits between executescript calls.
            await conn.execute(f"PRAGMA user_version = {version}")
            await conn.commit()

        self._log.info(
            "storage.migration.complete",
            schema_version=_SCHEMA_VERSION,
        )

    async def close(self) -> None:
        """Flush WAL and close connection."""
        async with self._get_lock():
            if self._conn is None:
                return
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await self._conn.close()
            self._conn = None
            self._log.info("storage.closed")

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            # SCAN3-011: structured log before raising so the error is queryable
            # in aggregators even when the exception is caught broadly upstream.
            log.critical(
                "storage.not_initialized",
                action="call await storage.initialize() before any storage operation",
            )
            raise RuntimeError("StorageBackend not initialized — call await backend.initialize()")
        return self._conn

    @asynccontextmanager
    async def _bulk_write_ctx(self) -> AsyncIterator[None]:
        """
        Temporarily lowers synchronous=NORMAL for bulk non-financial writes
        (bar upserts, equity snapshots) and restores FULL afterwards.

        C-09: PRAGMA must be set INSIDE self._lock to prevent financial writes
        (insert_trade, update_trade_exit) from accidentally executing under
        NORMAL durability when a concurrent bulk write is in progress.
        The lock is held for the full duration of the bulk write.
        """
        conn = self._require_conn()
        async with self._get_lock():
            await conn.execute("PRAGMA synchronous=NORMAL")
            try:
                yield
            finally:
                await conn.execute("PRAGMA synchronous=FULL")

    # ------------------------------------------------------------------
    # Bars
    # ------------------------------------------------------------------

    async def upsert_bars(self, bars: list[BarRecord]) -> int:
        """
        Insert or ignore OHLCV bars.  Returns number of new rows written.
        UNIQUE(symbol, timeframe, ts) prevents duplicates on re-fetch.
        """
        if not bars:
            return 0
        conn = self._require_conn()
        rows = [
            (
                b.symbol,
                b.timeframe,
                b.ts,
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                b.quote_volume,
                b.taker_buy_vol,
            )
            for b in bars
        ]
        # C-09: _bulk_write_ctx now holds self._lock internally — no inner lock needed
        async with self._bulk_write_ctx():
            cursor = await conn.executemany(
                """
                INSERT OR IGNORE INTO bars
                  (symbol, timeframe, ts, open, high, low, close,
                   volume, quote_volume, taker_buy_vol)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
            await conn.commit()
            inserted = cursor.rowcount if cursor.rowcount >= 0 else len(rows)
        self._log.debug("bars.upserted", count=inserted, symbol=bars[0].symbol)
        return inserted

    async def fetch_bars(
        self,
        symbol: str,
        timeframe: str,
        since_ts: int,
        limit: int = 2000,
    ) -> list[BarRecord]:
        """
        Return bars for symbol/timeframe at or after since_ts (Unix ms),
        ordered ascending, capped at limit rows.

        SCAN2-004: read-only — no lock needed under SQLite WAL mode.
        Concurrent readers with one writer are safe at the filesystem level.
        """
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT symbol, timeframe, ts, open, high, low, close,
                   volume, quote_volume, taker_buy_vol
            FROM bars
            WHERE symbol=? AND timeframe=? AND ts>=?
            ORDER BY ts ASC
            LIMIT ?
            """,
            (symbol, timeframe, since_ts, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [
            BarRecord(
                symbol=r["symbol"],
                timeframe=r["timeframe"],
                ts=r["ts"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
                quote_volume=r["quote_volume"],
                taker_buy_vol=r["taker_buy_vol"],
            )
            for r in rows
        ]

    # -----------------------------------------------------------------------
    # Intelligence features history (GAP-015)
    # -----------------------------------------------------------------------

    async def store_intelligence_features(
        self,
        symbol: str,
        timeframe: str,
        bar_ts: int,
        features: dict,
        confidence: float,
        source: str = "backfill",
    ) -> None:
        """
        Upsert one row into intelligence_features_history.

        Args:
            symbol:     Asset symbol, e.g. "BTCUSDT".
            timeframe:  Timeframe string, e.g. "1h".
            bar_ts:     Bar timestamp, Unix ms (matches bars.ts).
            features:   Dict keyed by the 18 intelligence column names.
                        Missing keys are stored as NULL (acceptable — coverage
                        check in trainer will flag low-coverage columns).
            confidence: Provider confidence score [0.0, 1.0].
            source:     "backfill" | "live" | "test".
        """
        from datetime import UTC, datetime as _dt

        conn = self._require_conn()
        fetched_at = int(_dt.now(UTC).timestamp() * 1000)

        def _f(key: str):
            v = features.get(key)
            return float(v) if v is not None else None

        await conn.execute(
            """
            INSERT OR REPLACE INTO intelligence_features_history (
                symbol, timeframe, bar_ts, fetched_at,
                exchange_netflow_7d_zscore, whale_buy_sell_ratio,
                exchange_reserve_ratio, miner_netflow_signal,
                staking_unlock_risk, entity_exchange_imbalance,
                binance_funding_rate_pct, liquidation_pressure_24h_zscore,
                futures_oi_change_pct, liquidation_cascade_risk_usd,
                btc_dominance_regime, stablecoin_reserve_ratio,
                network_activity_score, exchange_stress_score,
                cross_exchange_basis_spread_bps,
                defi_tvl_7d_change_pct, mvrv_z_score, sopr,
                confidence, source
            ) VALUES (
                ?,?,?,?,  ?,?,?,?,?,?,  ?,?,?,?,  ?,?,?,?,?,  ?,?,?,  ?,?
            )
            """,
            (
                symbol,
                timeframe,
                bar_ts,
                fetched_at,
                _f("intelligence_exchange_netflow_7d_zscore"),
                _f("intelligence_whale_buy_sell_ratio"),
                _f("intelligence_exchange_reserve_ratio"),
                _f("intelligence_miner_netflow_signal"),
                _f("intelligence_staking_unlock_risk"),
                _f("intelligence_entity_exchange_imbalance"),
                _f("intelligence_binance_funding_rate_pct"),
                _f("intelligence_liquidation_pressure_24h_zscore"),
                _f("intelligence_futures_oi_change_pct"),
                _f("intelligence_liquidation_cascade_risk_usd"),
                _f("intelligence_btc_dominance_regime"),
                _f("intelligence_stablecoin_reserve_ratio"),
                _f("intelligence_network_activity_score"),
                _f("intelligence_exchange_stress_score"),
                _f("intelligence_cross_exchange_basis_spread_bps"),
                _f("intelligence_defi_tvl_7d_change_pct"),
                _f("intelligence_mvrv_z_score"),
                _f("intelligence_sopr"),
                float(confidence),
                source,
            ),
        )
        await conn.commit()

    async def fetch_intelligence_features(
        self,
        symbol: str,
        timeframe: str,
        since_ts: int = 0,
        limit: int = 100_000,
    ) -> pd.DataFrame:
        """
        Fetch intelligence_features_history as a DataFrame aligned by bar_ts.

        Returns:
            DataFrame indexed by bar_ts (Unix ms), columns are the 18
            intelligence_* feature names + "confidence". Rows with all-NULL
            features are included so the caller can compute coverage per column.

        Usage in trainer (GAP-015 step 4):
            intel_df = await storage.fetch_intelligence_features(symbol, tf)
            feature_df = feature_df.join(intel_df, on="ts", how="left")
        """
        import pandas as _pd

        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT bar_ts,
                   exchange_netflow_7d_zscore,     whale_buy_sell_ratio,
                   exchange_reserve_ratio,          miner_netflow_signal,
                   staking_unlock_risk,             entity_exchange_imbalance,
                   binance_funding_rate_pct,        liquidation_pressure_24h_zscore,
                   futures_oi_change_pct,           liquidation_cascade_risk_usd,
                   btc_dominance_regime,            stablecoin_reserve_ratio,
                   network_activity_score,          exchange_stress_score,
                   cross_exchange_basis_spread_bps,
                   defi_tvl_7d_change_pct,          mvrv_z_score,  sopr,
                   confidence
            FROM intelligence_features_history
            WHERE symbol=? AND timeframe=? AND bar_ts>=?
            ORDER BY bar_ts ASC
            LIMIT ?
            """,
            (symbol, timeframe, since_ts, limit),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return _pd.DataFrame()

        col_map = {
            "exchange_netflow_7d_zscore": "intelligence_exchange_netflow_7d_zscore",
            "whale_buy_sell_ratio": "intelligence_whale_buy_sell_ratio",
            "exchange_reserve_ratio": "intelligence_exchange_reserve_ratio",
            "miner_netflow_signal": "intelligence_miner_netflow_signal",
            "staking_unlock_risk": "intelligence_staking_unlock_risk",
            "entity_exchange_imbalance": "intelligence_entity_exchange_imbalance",
            "binance_funding_rate_pct": "intelligence_binance_funding_rate_pct",
            "liquidation_pressure_24h_zscore": "intelligence_liquidation_pressure_24h_zscore",
            "futures_oi_change_pct": "intelligence_futures_oi_change_pct",
            "liquidation_cascade_risk_usd": "intelligence_liquidation_cascade_risk_usd",
            "btc_dominance_regime": "intelligence_btc_dominance_regime",
            "stablecoin_reserve_ratio": "intelligence_stablecoin_reserve_ratio",
            "network_activity_score": "intelligence_network_activity_score",
            "exchange_stress_score": "intelligence_exchange_stress_score",
            "cross_exchange_basis_spread_bps": "intelligence_cross_exchange_basis_spread_bps",
            # OCI-012 new columns
            "defi_tvl_7d_change_pct": "intelligence_defi_tvl_7d_change_pct",
            "mvrv_z_score": "intelligence_mvrv_z_score",
            "sopr": "intelligence_sopr",
            "confidence": "intelligence_confidence",
        }

        df = (
            _pd.DataFrame(
                [dict(r) for r in rows],
            )
            .rename(columns=col_map)
            .set_index("bar_ts")
        )
        return df

    async def intelligence_feature_coverage(
        self,
        symbol: str,
        timeframe: str,
    ) -> dict:
        """
        Return per-column non-NULL fraction for intelligence_features_history.

        Used by trainer before accepting the full intelligence feature matrix — any column
        with coverage < threshold should be dropped rather than trained on.

        Returns:
            {
                "total_rows": int,
                "coverage": {"intelligence_<col>": float, ...}   # 0.0-1.0
            }
        """
        conn = self._require_conn()
        columns = [
            "exchange_netflow_7d_zscore",
            "whale_buy_sell_ratio",
            "exchange_reserve_ratio",
            "miner_netflow_signal",
            "staking_unlock_risk",
            "entity_exchange_imbalance",
            "binance_funding_rate_pct",
            "liquidation_pressure_24h_zscore",
            "futures_oi_change_pct",
            "liquidation_cascade_risk_usd",
            "btc_dominance_regime",
            "stablecoin_reserve_ratio",
            "network_activity_score",
            "exchange_stress_score",
            "cross_exchange_basis_spread_bps",
            # OCI-012 new columns
            "defi_tvl_7d_change_pct",
            "mvrv_z_score",
            "sopr",
        ]
        count_exprs = ", ".join(
            f"SUM(CASE WHEN {c} IS NOT NULL THEN 1 ELSE 0 END) AS {c}" for c in columns
        )
        # count_exprs is built only from the hardcoded `columns` list literal
        # above, never from external/user input.
        async with conn.execute(
            f"SELECT COUNT(*) AS total, {count_exprs} "  # nosec B608
            "FROM intelligence_features_history WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ) as cur:
            _fetched = await cur.fetchone()
            row = dict(_fetched) if _fetched is not None else {}

        total = int(row.get("total") or 0)
        if total == 0:
            return {"total_rows": 0, "coverage": {}}

        prefix = "intelligence_"
        coverage = {f"{prefix}{c}": round(float(row.get(c) or 0) / total, 4) for c in columns}
        return {"total_rows": total, "coverage": coverage}

    async def latest_bar_ts(self, symbol: str, timeframe: str) -> int | None:
        """Return the most recent bar timestamp (Unix ms) or None if no data."""
        conn = self._require_conn()
        async with conn.execute(
            "SELECT MAX(ts) FROM bars WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    async def latest_close(self, symbol: str, timeframe: str) -> tuple[int, float] | None:
        """
        GAP-005/GAP-015: return (ts, close) for the single most recent bar.

        Cheap, single-row read used by the orchestrator each tick to feed
        PortfolioCorrelationTracker.push_bar_returns() — avoids loading a
        full bars DataFrame (fetch_bars) just to compute one bar's return.
        Returns None if no bars are stored yet for this symbol/timeframe.
        """
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT ts, close FROM bars
            WHERE symbol=? AND timeframe=?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (symbol, timeframe),
        ) as cur:
            row = await cur.fetchone()
        if row is None or row["ts"] is None or row["close"] is None:
            return None
        return (int(row["ts"]), float(row["close"]))

    async def bar_count(self, symbol: str, timeframe: str) -> int:
        """Return total stored bar count for a symbol/timeframe."""
        conn = self._require_conn()
        async with conn.execute(
            "SELECT COUNT(*) FROM bars WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def prune_old_bars(self, symbol: str, timeframe: str, keep_days: int) -> int:
        """
        Delete bars older than keep_days to cap storage.
        Returns count of deleted rows.
        """
        conn = self._require_conn()
        cutoff_ms = int((datetime.now(tz=UTC).timestamp() - keep_days * 86400) * 1000)
        async with self._get_lock():
            cursor = await conn.execute(
                "DELETE FROM bars WHERE symbol=? AND timeframe=? AND ts<?",
                (symbol, timeframe, cutoff_ms),
            )
            await conn.commit()
        deleted = cursor.rowcount if cursor.rowcount >= 0 else 0
        self._log.info(
            "bars.pruned",
            symbol=symbol,
            timeframe=timeframe,
            deleted=deleted,
            keep_days=keep_days,
        )
        return deleted

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    async def insert_trade(self, trade: TradeRecord) -> None:
        """Insert a new trade record.  Raises ValueError if id already exists."""
        conn = self._require_conn()
        async with self._get_lock():
            try:
                await conn.execute(
                    """
                    INSERT INTO trades (
                        id, symbol, timeframe, trading_mode, execution_mode,
                        direction, entry_price, exit_price, quantity, notional_usd,
                        entry_ts, exit_ts, pnl_usd, pnl_pct, fee_usd,
                        kelly_fraction, regime_at_entry, meta_label_prob,
                        exit_reason, approved_by, raw_signal
                    ) VALUES (
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    """,
                    (
                        trade.id,
                        trade.symbol,
                        trade.timeframe,
                        trade.trading_mode,
                        trade.execution_mode,
                        trade.direction,
                        trade.entry_price,
                        trade.exit_price,
                        trade.quantity,
                        trade.notional_usd,
                        trade.entry_ts,
                        trade.exit_ts,
                        trade.pnl_usd,
                        trade.pnl_pct,
                        trade.fee_usd,
                        trade.kelly_fraction,
                        trade.regime_at_entry,
                        trade.meta_label_prob,
                        trade.exit_reason,
                        trade.approved_by,
                        trade.raw_signal,
                    ),
                )
                await conn.commit()
            except aiosqlite.IntegrityError as exc:
                # BUGFIX-001: must roll back the failed INSERT before releasing
                # the lock — otherwise the connection is left holding an open
                # transaction on the trades table, deadlocking every subsequent
                # write (including the WAL checkpoint on close()).
                await conn.rollback()
                raise ValueError(f"Trade id={trade.id!r} already exists") from exc
        self._log.info(
            "trade.inserted",
            trade_id=trade.id,
            symbol=trade.symbol,
            direction=trade.direction,
        )

    async def update_trade_exit(
        self,
        trade_id: str,
        exit_price: float,
        exit_ts: int,
        pnl_usd: float,
        pnl_pct: float,
        exit_reason: str,
        fee_usd: float,
    ) -> None:
        """Patch exit fields on an existing trade row."""
        conn = self._require_conn()
        async with self._get_lock():
            cursor = await conn.execute(
                """
                UPDATE trades
                SET exit_price=?, exit_ts=?, pnl_usd=?, pnl_pct=?,
                    exit_reason=?, fee_usd=fee_usd+?
                WHERE id=? AND exit_ts IS NULL
                """,
                # SCAN2-002: fee_usd+? intentionally accumulates entry_fee (stored at
                # insert_trade) + exit_fee_usd (passed here). The parameter is named
                # exit_fee_usd at the call site to prevent misreading as total fee.
                # AND exit_ts IS NULL prevents double-exit writes on the same trade.
                (exit_price, exit_ts, pnl_usd, pnl_pct, exit_reason, fee_usd, trade_id),
            )
            await conn.commit()
        if cursor.rowcount == 0:
            raise ValueError(
                f"No open trade found with id={trade_id!r} (already closed or id not found)"
            )
        self._log.info(
            "trade.exit_updated",
            trade_id=trade_id,
            pnl_usd=pnl_usd,
            exit_reason=exit_reason,
        )

    async def fetch_trades(
        self,
        symbol: str | None = None,
        trading_mode: str | None = None,
        since_ts: int | None = None,
        limit: int = 500,
        offset: int = 0,
        open_only: bool = False,
    ) -> list[TradeRecord]:
        """
        Fetch trades with optional filters.
        Returns list ordered by entry_ts descending.

        `open_only` restricts the result to trades with no exit recorded.
        After a crash these are the positions the database still believes
        are live, which is what src/diagnostics/disaster_recovery.py
        reconciles against the executor's in-memory book.
        """
        conn = self._require_conn()
        # Build parameterized query from fixed literal clause fragments only —
        # no user-supplied strings ever reach the SQL text.
        clauses: list[str] = []
        params: list[object] = []
        if symbol is not None:
            clauses.append("symbol=?")
            params.append(symbol)
        if trading_mode is not None:
            clauses.append("trading_mode=?")
            params.append(trading_mode)
        if since_ts is not None:
            clauses.append("entry_ts>=?")
            params.append(since_ts)
        if open_only:
            # No parameter: a literal IS NULL cannot be bound as a value.
            clauses.append("exit_ts IS NULL")
        # VF-010: Replaced f-string SQL composition with explicit conditional
        # query building — no variable is ever interpolated into the SQL text.
        # All filter values flow through ? placeholders only.
        if clauses:
            base_query = (
                "SELECT id, symbol, timeframe, trading_mode, execution_mode,"  # nosec B608 — clauses are hardcoded strings; values use ? placeholders
                " direction, entry_price, exit_price, quantity, notional_usd,"
                " entry_ts, exit_ts, pnl_usd, pnl_pct, fee_usd,"
                " kelly_fraction, regime_at_entry, meta_label_prob,"
                " exit_reason, approved_by, raw_signal"
                " FROM trades WHERE "
                + " AND ".join(clauses)
                + " ORDER BY entry_ts DESC LIMIT ? OFFSET ?"
            )
        else:
            base_query = (
                "SELECT id, symbol, timeframe, trading_mode, execution_mode,"
                " direction, entry_price, exit_price, quantity, notional_usd,"
                " entry_ts, exit_ts, pnl_usd, pnl_pct, fee_usd,"
                " kelly_fraction, regime_at_entry, meta_label_prob,"
                " exit_reason, approved_by, raw_signal"
                " FROM trades ORDER BY entry_ts DESC LIMIT ? OFFSET ?"
            )
        params.append(limit)
        params.append(offset)
        async with conn.execute(base_query, params) as cur:
            rows = await cur.fetchall()
        return [
            TradeRecord(
                id=r["id"],
                symbol=r["symbol"],
                timeframe=r["timeframe"],
                trading_mode=r["trading_mode"],
                execution_mode=r["execution_mode"],
                direction=r["direction"],
                entry_price=r["entry_price"],
                exit_price=r["exit_price"],
                quantity=r["quantity"],
                notional_usd=r["notional_usd"],
                entry_ts=r["entry_ts"],
                exit_ts=r["exit_ts"],
                pnl_usd=r["pnl_usd"],
                pnl_pct=r["pnl_pct"],
                fee_usd=r["fee_usd"],
                kelly_fraction=r["kelly_fraction"],
                regime_at_entry=r["regime_at_entry"],
                meta_label_prob=r["meta_label_prob"],
                exit_reason=r["exit_reason"],
                approved_by=r["approved_by"],
                raw_signal=r["raw_signal"],
            )
            for r in rows
        ]

    async def count_consecutive_losses(self, symbol: str, trading_mode: str) -> int:
        """
        Count trailing consecutive losing trades for the risk gate.
        Reads from most-recent backward; stops at first non-negative PnL.
        """
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT pnl_usd FROM trades
            WHERE symbol=? AND trading_mode=? AND pnl_usd IS NOT NULL
            ORDER BY entry_ts DESC
            LIMIT 20
            """,
            (symbol, trading_mode),
        ) as cur:
            rows = await cur.fetchall()
        streak = 0
        for row in rows:
            if row["pnl_usd"] is not None and row["pnl_usd"] < 0.0:
                streak += 1
            else:
                break
        return streak

    async def daily_pnl(self, symbol: str, trading_mode: str) -> float:
        """
        Sum realized PnL for today (UTC calendar day) for the risk gate.
        Uses exit_ts so only trades closed today are counted.
        """
        conn = self._require_conn()
        day_start_ms = int(
            datetime.now(tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            * 1000
        )
        async with conn.execute(
            """
            SELECT COALESCE(SUM(pnl_usd), 0.0) FROM trades
            WHERE symbol=? AND trading_mode=? AND exit_ts>=? AND pnl_usd IS NOT NULL
            """,
            (symbol, trading_mode, day_start_ms),
        ) as cur:
            row = await cur.fetchone()
        return float(row[0]) if row else 0.0

    # ------------------------------------------------------------------
    # Regime snapshots
    # ------------------------------------------------------------------

    async def upsert_regime_snapshot(self, snap: RegimeSnapshotRecord) -> None:
        """Insert or replace regime state at a bar timestamp."""
        conn = self._require_conn()
        async with self._get_lock():
            await conn.execute(
                """
                INSERT OR REPLACE INTO regime_snapshots
                  (symbol, timeframe, ts, regime_state,
                   prob_ranging, prob_trending, prob_volatile,
                   changepoint_probability, agreement_score)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    snap.symbol,
                    snap.timeframe,
                    snap.ts,
                    snap.regime_state,
                    snap.prob_ranging,
                    snap.prob_trending,
                    snap.prob_volatile,
                    snap.changepoint_probability,
                    snap.agreement_score,
                ),
            )
            await conn.commit()

    async def latest_regime(self, symbol: str, timeframe: str) -> RegimeSnapshotRecord | None:
        """Return the most recent regime snapshot or None."""
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT symbol, timeframe, ts, regime_state,
                   prob_ranging, prob_trending, prob_volatile,
                   changepoint_probability, agreement_score
            FROM regime_snapshots
            WHERE symbol=? AND timeframe=?
            ORDER BY ts DESC LIMIT 1
            """,
            (symbol, timeframe),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return RegimeSnapshotRecord(
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            ts=row["ts"],
            regime_state=row["regime_state"],
            prob_ranging=row["prob_ranging"],
            prob_trending=row["prob_trending"],
            prob_volatile=row["prob_volatile"],
            changepoint_probability=row["changepoint_probability"],
            agreement_score=row["agreement_score"],
        )

    async def regime_snapshot_before(
        self, symbol: str, timeframe: str, ts: int
    ) -> RegimeSnapshotRecord | None:
        """Most recent regime snapshot at or before `ts` — used by the
        self-tuning scheduler to recover the posterior entropy that was
        live at a historical trade's entry time (regime_snapshots is the
        only place that probability triple is persisted)."""
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT symbol, timeframe, ts, regime_state,
                   prob_ranging, prob_trending, prob_volatile,
                   changepoint_probability, agreement_score
            FROM regime_snapshots
            WHERE symbol=? AND timeframe=? AND ts<=?
            ORDER BY ts DESC LIMIT 1
            """,
            (symbol, timeframe, ts),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return RegimeSnapshotRecord(
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            ts=row["ts"],
            regime_state=row["regime_state"],
            prob_ranging=row["prob_ranging"],
            prob_trending=row["prob_trending"],
            prob_volatile=row["prob_volatile"],
            changepoint_probability=row["changepoint_probability"],
            agreement_score=row["agreement_score"],
        )

    async def bars_before(
        self, symbol: str, timeframe: str, ts: int, limit: int = 21
    ) -> list[BarRecord]:
        """Most recent `limit` bars at or before `ts`, ascending order --
        used by the self-tuning scheduler to reconstruct the reference
        price and ADV-20d that were live at a historical trade's entry
        time (see Phase 8 slippage-coefficient recalibration)."""
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT symbol, timeframe, ts, open, high, low, close,
                   volume, quote_volume, taker_buy_vol
            FROM (
                SELECT symbol, timeframe, ts, open, high, low, close,
                       volume, quote_volume, taker_buy_vol
                FROM bars
                WHERE symbol=? AND timeframe=? AND ts<=?
                ORDER BY ts DESC LIMIT ?
            )
            ORDER BY ts ASC
            """,
            (symbol, timeframe, ts, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [
            BarRecord(
                symbol=r["symbol"],
                timeframe=r["timeframe"],
                ts=r["ts"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
                quote_volume=r["quote_volume"],
                taker_buy_vol=r["taker_buy_vol"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Missed trades (UI-001)
    # ------------------------------------------------------------------

    async def insert_missed_trade(self, record: MissedTradeRecord) -> None:
        """Best-effort log of a tradeable signal that never opened a
        position. Never raises into the trading path — callers should
        wrap this in try/except, matching the metrics-push pattern
        elsewhere in the orchestrator."""
        conn = self._require_conn()
        async with self._get_lock():
            await conn.execute(
                """
                INSERT OR IGNORE INTO missed_trades (
                    id, symbol, timeframe, direction, reason,
                    kelly_fraction, meta_label_prob, raw_signal,
                    regime_at_entry, notional_usd, ts
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.id,
                    record.symbol,
                    record.timeframe,
                    record.direction,
                    record.reason,
                    record.kelly_fraction,
                    record.meta_label_prob,
                    record.raw_signal,
                    record.regime_at_entry,
                    record.notional_usd,
                    record.ts,
                ),
            )
            await conn.commit()

    async def fetch_missed_trades(
        self, symbol: str | None = None, limit: int = 50
    ) -> list[MissedTradeRecord]:
        """Most recent missed trades, newest first."""
        conn = self._require_conn()
        clauses: list[str] = []
        params: list[object] = []
        if symbol is not None:
            clauses.append("symbol=?")
            params.append(symbol)
        base_query = (
            "SELECT id, symbol, timeframe, direction, reason,"  # nosec B608 — clauses are hardcoded strings; values use ? placeholders
            " kelly_fraction, meta_label_prob, raw_signal, regime_at_entry,"
            " notional_usd, ts FROM missed_trades"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY ts DESC LIMIT ?"
        )
        params.append(limit)
        async with conn.execute(base_query, params) as cur:
            rows = await cur.fetchall()
        return [
            MissedTradeRecord(
                id=r["id"],
                symbol=r["symbol"],
                timeframe=r["timeframe"],
                direction=r["direction"],
                reason=r["reason"],
                kelly_fraction=r["kelly_fraction"],
                meta_label_prob=r["meta_label_prob"],
                raw_signal=r["raw_signal"],
                regime_at_entry=r["regime_at_entry"],
                notional_usd=r["notional_usd"],
                ts=r["ts"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Model metrics
    # ------------------------------------------------------------------

    async def insert_model_metrics(self, metrics: ModelMetricsRecord) -> None:
        """Persist a CPCV OOS evaluation result."""
        conn = self._require_conn()
        async with self._get_lock():
            await conn.execute(
                """
                INSERT OR REPLACE INTO model_metrics
                  (model_name, timeframe, version, oos_sharpe, max_drawdown,
                   n_trades, accuracy, precision_score, recall_score,
                   f1_score, live_gate_pass)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    metrics.model_name,
                    metrics.timeframe,
                    metrics.version,
                    metrics.oos_sharpe,
                    metrics.max_drawdown,
                    metrics.n_trades,
                    metrics.accuracy,
                    metrics.precision_score,
                    metrics.recall_score,
                    metrics.f1_score,
                    int(metrics.live_gate_pass),
                ),
            )
            await conn.commit()
        self._log.info(
            "model_metrics.inserted",
            model=metrics.model_name,
            timeframe=metrics.timeframe,
            version=metrics.version,
            live_gate_pass=metrics.live_gate_pass,
        )

    async def latest_model_metrics(
        self, model_name: str, timeframe: str
    ) -> ModelMetricsRecord | None:
        """Return the most recent metrics row for a model/timeframe pair."""
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT model_name, timeframe, version, oos_sharpe, max_drawdown,
                   n_trades, accuracy, precision_score, recall_score,
                   f1_score, live_gate_pass
            FROM model_metrics
            WHERE model_name=? AND timeframe=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (model_name, timeframe),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return ModelMetricsRecord(
            model_name=row["model_name"],
            timeframe=row["timeframe"],
            version=row["version"],
            oos_sharpe=row["oos_sharpe"],
            max_drawdown=row["max_drawdown"],
            n_trades=row["n_trades"],
            accuracy=row["accuracy"],
            precision_score=row["precision_score"],
            recall_score=row["recall_score"],
            f1_score=row["f1_score"],
            live_gate_pass=bool(row["live_gate_pass"]),
        )

    async def live_gate_passes(self, timeframe: str) -> bool:
        """
        True only when BOTH direction and meta_label models
        have live_gate_pass=1 for the given timeframe.
        """
        direction = await self.latest_model_metrics("direction", timeframe)
        meta = await self.latest_model_metrics("meta_label", timeframe)
        if direction is None or meta is None:
            return False
        return direction.live_gate_pass and meta.live_gate_pass

    # ------------------------------------------------------------------
    # Equity curve
    # ------------------------------------------------------------------

    async def insert_equity(self, record: EquityRecord) -> None:
        """Insert a point-in-time equity snapshot."""
        conn = self._require_conn()
        # C-09: _bulk_write_ctx now holds self._lock internally — no inner lock needed
        async with self._bulk_write_ctx():
            await conn.execute(
                """
                INSERT OR REPLACE INTO equity_curve
                  (ts, trading_mode, equity_usd, cash_usd, unrealized_pnl,
                   daily_pnl_usd, daily_pnl_pct, peak_equity_usd, drawdown_pct)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.ts,
                    record.trading_mode,
                    record.equity_usd,
                    record.cash_usd,
                    record.unrealized_pnl,
                    record.daily_pnl_usd,
                    record.daily_pnl_pct,
                    record.peak_equity_usd,
                    record.drawdown_pct,
                ),
            )
            await conn.commit()

    async def fetch_equity_curve(
        self,
        trading_mode: str,
        since_ts: int | None = None,
        limit: int = 1440,
    ) -> list[EquityRecord]:
        """Return equity snapshots in ascending time order."""
        conn = self._require_conn()
        # C-06: use safe clause-list pattern — no f-string interpolation of any
        # variable into the SQL text, even though ts_clause was previously
        # a hardcoded literal (safe pattern prevents future injection).
        clauses = ["trading_mode=?"]
        params: list[object] = [trading_mode]
        # VF-008: Replaced f-string SQL composition with explicit conditional
        # query building — no variable ever reaches the SQL text.
        if since_ts is not None:
            clauses.append("ts>=?")
            params.append(since_ts)
        params.append(limit)
        if len(clauses) > 1:
            equity_query = (
                "SELECT ts, trading_mode, equity_usd, cash_usd, unrealized_pnl,"
                " daily_pnl_usd, daily_pnl_pct, peak_equity_usd, drawdown_pct"
                " FROM equity_curve WHERE trading_mode=? AND ts>=?"
                " ORDER BY ts ASC LIMIT ?"
            )
        else:
            equity_query = (
                "SELECT ts, trading_mode, equity_usd, cash_usd, unrealized_pnl,"
                " daily_pnl_usd, daily_pnl_pct, peak_equity_usd, drawdown_pct"
                " FROM equity_curve WHERE trading_mode=?"
                " ORDER BY ts ASC LIMIT ?"
            )
        async with conn.execute(equity_query, params) as cur:
            rows = await cur.fetchall()
        return [
            EquityRecord(
                ts=r["ts"],
                trading_mode=r["trading_mode"],
                equity_usd=r["equity_usd"],
                cash_usd=r["cash_usd"],
                unrealized_pnl=r["unrealized_pnl"],
                daily_pnl_usd=r["daily_pnl_usd"],
                daily_pnl_pct=r["daily_pnl_pct"],
                peak_equity_usd=r["peak_equity_usd"],
                drawdown_pct=r["drawdown_pct"],
            )
            for r in rows
        ]

    async def latest_equity(self, trading_mode: str) -> EquityRecord | None:
        """Return most recent equity snapshot or None."""
        conn = self._require_conn()
        async with conn.execute(
            """
            SELECT ts, trading_mode, equity_usd, cash_usd, unrealized_pnl,
                   daily_pnl_usd, daily_pnl_pct, peak_equity_usd, drawdown_pct
            FROM equity_curve
            WHERE trading_mode=?
            ORDER BY ts DESC LIMIT 1
            """,
            (trading_mode,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return EquityRecord(
            ts=row["ts"],
            trading_mode=row["trading_mode"],
            equity_usd=row["equity_usd"],
            cash_usd=row["cash_usd"],
            unrealized_pnl=row["unrealized_pnl"],
            daily_pnl_usd=row["daily_pnl_usd"],
            daily_pnl_pct=row["daily_pnl_pct"],
            peak_equity_usd=row["peak_equity_usd"],
            drawdown_pct=row["drawdown_pct"],
        )

    async def earliest_equity_ts(self, trading_mode: str) -> int | None:
        """Return the earliest equity_curve timestamp (ms) for a trading mode, or None."""
        conn = self._require_conn()
        async with conn.execute(
            "SELECT MIN(ts) AS earliest FROM equity_curve WHERE trading_mode=?",
            (trading_mode,),
        ) as cur:
            row = await cur.fetchone()
        if row is None or row["earliest"] is None:
            return None
        return int(row["earliest"])

    async def validate_symbol(self, symbol: str) -> None:
        """
        Raise ValueError if symbol is not a known trading pair in storage.

        Checks format first (e.g. 'BTC/USDT'), then existence in bars table.
        Prevents user-supplied symbol strings from reaching raw SQL.
        """
        if not _SYMBOL_RE.match(symbol):
            raise ValueError(
                f"Invalid symbol format {symbol!r}. Expected format: BASE/QUOTE (e.g. BTC/USDT)"
            )
        conn = self._require_conn()
        async with conn.execute(
            "SELECT COUNT(*) FROM bars WHERE symbol=? LIMIT 1",
            (symbol,),
        ) as cur:
            row = await cur.fetchone()
        if not row or int(row[0]) == 0:
            raise ValueError(f"Unknown symbol {symbol!r} — not found in stored bars")

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    async def insert_audit_event(
        self,
        event_type: str,
        operator: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Persist an audit event (e.g. execution mode change) to audit_log."""
        conn = self._require_conn()
        details_json = json.dumps(details or {})
        async with self._get_lock():
            await conn.execute(
                """
                INSERT INTO audit_log (event_type, operator, details)
                VALUES (?, ?, ?)
                """,
                (event_type, operator, details_json),
            )
            await conn.commit()
        self._log.info(
            "audit.event",
            event_type=event_type,
            operator=operator,
            details=details or {},
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self) -> dict[str, object]:
        """Return row counts per table — used by API health endpoint."""
        conn = self._require_conn()
        counts: dict[str, object] = {}
        # VF-009: Defence-in-depth: validate each table name against a safe-chars
        # pattern before interpolating into the f-string.  The loop variable comes
        # FROM _ALLOWED_TABLES (membership is already guaranteed), but a secondary
        # regex check catches any future misconfiguration where an unsafe value
        # (e.g. containing spaces, quotes, or SQL keywords) was accidentally added
        # to the allowlist itself.  The previous guard (table not in _ALLOWED_TABLES)
        # was logically dead — it could never be True since table is drawn from the
        # same set — and has been replaced with this character-level validation.
        _SAFE_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
        for table in _ALLOWED_TABLES:
            if not _SAFE_TABLE_RE.match(table):
                raise RuntimeError(f"health_check: table {table!r} contains unsafe characters")
            async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:  # nosec B608
                row = await cur.fetchone()
            counts[table] = int(row[0]) if row else 0
        return counts


# ---------------------------------------------------------------------------
# Backend factory + async context manager — preferred lifecycle management
# ---------------------------------------------------------------------------


def create_storage_backend(db_path: str | None = None) -> StorageBackend | TimescaleBackend:
    """
    GAP-006: construct the storage backend selected by STORAGE_BACKEND
    ("sqlite" default | "timescale" — local TimescaleDB container, see
    scripts/timescaledb.sh). Both classes expose an identical public
    interface; callers never need to know which one they hold.

    db_path applies to the sqlite backend only and forces sqlite when given
    (used by tests and ad-hoc tooling that point at a specific .db file).
    """
    if db_path is None and get_settings().storage.backend == "timescale":
        # Deferred import: asyncpg is only required when timescale is selected.
        from src.data.timescale_storage import TimescaleBackend

        return TimescaleBackend()
    return StorageBackend(db_path=db_path)


@asynccontextmanager
async def open_storage(
    db_path: str | None = None,
) -> AsyncIterator[StorageBackend | TimescaleBackend]:
    """
    Async context manager for the configured storage backend.

    Usage::

        async with open_storage() as storage:
            bars = await storage.fetch_bars("BTC/USDT", "15m", since_ts)
    """
    backend = create_storage_backend(db_path=db_path)
    await backend.initialize()
    try:
        yield backend
    finally:
        await backend.close()
