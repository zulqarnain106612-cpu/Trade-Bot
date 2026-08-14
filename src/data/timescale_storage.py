"""
GAP-006: Async TimescaleDB storage backend — asyncpg pool, hypertables, typed queries.

Interface parity with src.data.storage.StorageBackend (SQLite): every public
async method has the same name, signature, return type, and observable
semantics, so callers can hold either backend interchangeably.

Schema owns the same tables as the SQLite backend:
  bars            — OHLCV per symbol/timeframe (TimescaleDB hypertable)
  trades          — paper and live trade records with full audit trail
  regime_snapshots — HMM state at every bar
  model_metrics   — CPCV OOS metrics per model version
  equity_curve    — timestamped equity snapshots (TimescaleDB hypertable)
  audit_log       — operator/system audit events
  intelligence_features_history — GAP-015 backfill feature matrix

Design notes (GAP-006):
  - Timestamps stay Unix-ms BIGINT (never timestamptz) — every caller
    passes/expects Unix-ms ints; hypertables use an integer time column
    with unix_ms_now() as the integer-now function.
  - bars and equity_curve are hypertables (high-volume time series);
    trades / regime_snapshots / model_metrics / audit_log /
    intelligence_features_history stay regular tables (low volume or
    UPDATE-heavy).
  - Surrogate `id` columns on bars/equity_curve are dropped in the PG
    schema (hypertables require the partition column in every unique
    constraint; no caller reads those ids back).
  - Schema versioning uses a schema_version table instead of SQLite's
    PRAGMA user_version; migration semantics are identical (forward-only,
    skip applied, error if DB is ahead of code).

Authority sources:
  - asyncpg docs (https://magicstack.github.io/asyncpg/current/)
  - TimescaleDB hypertables (https://docs.timescale.com/use-timescale/latest/hypertables/)
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final


if TYPE_CHECKING:
    import pandas as pd

import asyncpg
import structlog

from src.config import get_settings
from src.data.storage import (
    _ALLOWED_TABLES,
    _SYMBOL_RE,
    BarRecord,
    EquityRecord,
    MissedTradeRecord,
    ModelMetricsRecord,
    RegimeSnapshotRecord,
    TradeRecord,
)


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# DDL — Postgres/TimescaleDB translation of the SQLite _DDL in storage.py
# ---------------------------------------------------------------------------

_PG_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Integer-now function required by TimescaleDB for BIGINT time columns.
CREATE OR REPLACE FUNCTION unix_ms_now() RETURNS BIGINT
LANGUAGE SQL STABLE AS
$$ SELECT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT $$;

CREATE TABLE IF NOT EXISTS bars (
    symbol          TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,
    ts              BIGINT  NOT NULL,   -- Unix ms, UTC
    open            DOUBLE PRECISION NOT NULL,
    high            DOUBLE PRECISION NOT NULL,
    low             DOUBLE PRECISION NOT NULL,
    close           DOUBLE PRECISION NOT NULL,
    volume          DOUBLE PRECISION NOT NULL,
    quote_volume    DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    taker_buy_vol   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    inserted_at     BIGINT  NOT NULL
        DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT,
    -- GAP-006: surrogate id dropped — hypertables require the partition
    -- column in every unique constraint; no caller reads bars.id.
    PRIMARY KEY (symbol, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_sym_tf_ts
    ON bars (symbol, timeframe, ts DESC);
CREATE INDEX IF NOT EXISTS idx_bars_sym_tf_ts_asc
    ON bars (symbol, timeframe, ts ASC);

CREATE TABLE IF NOT EXISTS trades (
    id              TEXT    PRIMARY KEY,  -- UUID
    symbol          TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,
    trading_mode    TEXT    NOT NULL,     -- paper | live
    execution_mode  TEXT    NOT NULL,     -- automatic | restricted | manual
    direction       INTEGER NOT NULL,     -- 1=long, 0=short
    entry_price     DOUBLE PRECISION NOT NULL,
    exit_price      DOUBLE PRECISION,
    quantity        DOUBLE PRECISION NOT NULL,
    notional_usd    DOUBLE PRECISION NOT NULL,
    entry_ts        BIGINT  NOT NULL,
    exit_ts         BIGINT,
    pnl_usd         DOUBLE PRECISION,
    pnl_pct         DOUBLE PRECISION,
    fee_usd         DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    kelly_fraction  DOUBLE PRECISION NOT NULL,
    regime_at_entry INTEGER NOT NULL,     -- 0=ranging,1=trending,2=volatile
    meta_label_prob DOUBLE PRECISION NOT NULL,
    exit_reason     TEXT,
    approved_by     TEXT,
    raw_signal      DOUBLE PRECISION,
    created_at      BIGINT  NOT NULL
        DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT
);
CREATE INDEX IF NOT EXISTS idx_trades_sym_ts
    ON trades (symbol, entry_ts DESC);
CREATE INDEX IF NOT EXISTS idx_trades_mode
    ON trades (trading_mode, entry_ts DESC);

CREATE TABLE IF NOT EXISTS regime_snapshots (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol                  TEXT    NOT NULL,
    timeframe               TEXT    NOT NULL,
    ts                      BIGINT  NOT NULL,
    regime_state            INTEGER NOT NULL,     -- 0|1|2
    prob_ranging            DOUBLE PRECISION NOT NULL,
    prob_trending           DOUBLE PRECISION NOT NULL,
    prob_volatile           DOUBLE PRECISION NOT NULL,
    changepoint_probability DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    agreement_score         DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    UNIQUE (symbol, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_regime_sym_tf_ts
    ON regime_snapshots (symbol, timeframe, ts DESC);

CREATE TABLE IF NOT EXISTS model_metrics (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    model_name      TEXT    NOT NULL,     -- 'direction' | 'meta_label'
    timeframe       TEXT    NOT NULL,
    version         TEXT    NOT NULL,     -- ISO timestamp of training run
    oos_sharpe      DOUBLE PRECISION NOT NULL,
    max_drawdown    DOUBLE PRECISION NOT NULL,
    n_trades        INTEGER NOT NULL,
    accuracy        DOUBLE PRECISION NOT NULL,
    precision_score DOUBLE PRECISION NOT NULL,
    recall_score    DOUBLE PRECISION NOT NULL,
    f1_score        DOUBLE PRECISION NOT NULL,
    live_gate_pass  INTEGER NOT NULL DEFAULT 0,  -- 1 if all thresholds met
    created_at      BIGINT  NOT NULL
        DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT,
    UNIQUE (model_name, timeframe, version)
);
CREATE INDEX IF NOT EXISTS idx_model_metrics_name_tf
    ON model_metrics (model_name, timeframe, created_at DESC);

CREATE TABLE IF NOT EXISTS equity_curve (
    ts              BIGINT  NOT NULL,
    trading_mode    TEXT    NOT NULL,
    equity_usd      DOUBLE PRECISION NOT NULL,
    cash_usd        DOUBLE PRECISION NOT NULL,
    unrealized_pnl  DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    daily_pnl_usd   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    daily_pnl_pct   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    peak_equity_usd DOUBLE PRECISION NOT NULL,
    drawdown_pct    DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    -- GAP-006: surrogate id dropped (see bars); UNIQUE(ts, trading_mode)
    -- from SCAN3-005 becomes the primary key.
    PRIMARY KEY (ts, trading_mode)
);
CREATE INDEX IF NOT EXISTS idx_equity_ts
    ON equity_curve (ts DESC);

CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts              BIGINT  NOT NULL
        DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT,
    event_type      TEXT    NOT NULL,  -- e.g. 'execution_mode_change'
    operator        TEXT    NOT NULL,
    details         TEXT    NOT NULL DEFAULT '{}'  -- JSON blob
);
CREATE INDEX IF NOT EXISTS idx_audit_ts
    ON audit_log (ts DESC);
"""

# Hypertable conversion — run statement-by-statement after _PG_DDL.
# chunk_time_interval is in ms (BIGINT time column); 604800000 = 7 days.
_HYPERTABLE_SQL: Final[tuple[str, ...]] = (
    "SELECT create_hypertable('bars', 'ts',"
    " chunk_time_interval => 604800000,"
    " if_not_exists => TRUE, migrate_data => TRUE);",
    "SELECT set_integer_now_func('bars', 'unix_ms_now', replace_if_exists => true);",
    "SELECT create_hypertable('equity_curve', 'ts',"
    " chunk_time_interval => 604800000,"
    " if_not_exists => TRUE, migrate_data => TRUE);",
    "SELECT set_integer_now_func('equity_curve', 'unix_ms_now', replace_if_exists => true);",
)

# ---------------------------------------------------------------------------
# Schema migrations — same semantics as storage._MIGRATIONS (Gap-012),
# tracked in the schema_version table instead of PRAGMA user_version.
# ---------------------------------------------------------------------------

_PG_MIGRATIONS: Final[list[tuple[int, str, str]]] = [
    (
        1,
        "initial schema: bars, trades, regime_snapshots, model_metrics, equity_curve, audit_log",
        "",  # Tables already exist from _PG_DDL; version marker only.
    ),
    (
        2,
        "add spread_bps to trades for slippage audit trail",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS spread_bps DOUBLE PRECISION;",
    ),
    (
        3,
        "gap-015: add intelligence_features_history table for backfill pipeline",
        """CREATE TABLE IF NOT EXISTS intelligence_features_history (
    symbol      TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    bar_ts      BIGINT  NOT NULL,
    fetched_at  BIGINT  NOT NULL,
    exchange_netflow_7d_zscore      DOUBLE PRECISION,
    whale_buy_sell_ratio            DOUBLE PRECISION,
    exchange_reserve_ratio          DOUBLE PRECISION,
    miner_netflow_signal            DOUBLE PRECISION,
    staking_unlock_risk             DOUBLE PRECISION,
    entity_exchange_imbalance       DOUBLE PRECISION,
    binance_funding_rate_pct        DOUBLE PRECISION,
    liquidation_pressure_24h_zscore DOUBLE PRECISION,
    futures_oi_change_pct           DOUBLE PRECISION,
    liquidation_cascade_risk_usd    DOUBLE PRECISION,
    btc_dominance_regime            DOUBLE PRECISION,
    stablecoin_reserve_ratio        DOUBLE PRECISION,
    network_activity_score          DOUBLE PRECISION,
    exchange_stress_score           DOUBLE PRECISION,
    cross_exchange_basis_spread_bps DOUBLE PRECISION,
    confidence  DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    source      TEXT NOT NULL DEFAULT 'backfill',
    PRIMARY KEY (symbol, timeframe, bar_ts)
);
CREATE INDEX IF NOT EXISTS idx_intel_hist_ts
    ON intelligence_features_history (symbol, timeframe, bar_ts ASC);""",
    ),
    (
        4,
        "oci-012: add defi_tvl/mvrv_z_score/sopr to intelligence_features_history",
        """ALTER TABLE intelligence_features_history
    ADD COLUMN IF NOT EXISTS defi_tvl_7d_change_pct DOUBLE PRECISION;
ALTER TABLE intelligence_features_history
    ADD COLUMN IF NOT EXISTS mvrv_z_score DOUBLE PRECISION;
ALTER TABLE intelligence_features_history
    ADD COLUMN IF NOT EXISTS sopr DOUBLE PRECISION;""",
    ),
    (
        5,
        "ui-001: add missed_trades table for the dashboard's Missed Trades tab",
        """CREATE TABLE IF NOT EXISTS missed_trades (
    id              TEXT    PRIMARY KEY,
    symbol          TEXT    NOT NULL,
    timeframe       TEXT    NOT NULL,
    direction       INTEGER NOT NULL,
    reason          TEXT    NOT NULL,
    kelly_fraction  DOUBLE PRECISION NOT NULL,
    meta_label_prob DOUBLE PRECISION NOT NULL,
    raw_signal      DOUBLE PRECISION,
    regime_at_entry INTEGER NOT NULL,
    notional_usd    DOUBLE PRECISION NOT NULL,
    ts              BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_missed_trades_ts
    ON missed_trades (ts DESC);""",
    ),
    (
        6,
        "regime-ensemble: add changepoint_probability and agreement_score to regime_snapshots",
        """ALTER TABLE regime_snapshots
    ADD COLUMN IF NOT EXISTS changepoint_probability DOUBLE PRECISION NOT NULL DEFAULT 0.0;
ALTER TABLE regime_snapshots
    ADD COLUMN IF NOT EXISTS agreement_score DOUBLE PRECISION NOT NULL DEFAULT 1.0;""",
    ),
]

_PG_SCHEMA_VERSION: Final[int] = len(_PG_MIGRATIONS)  # = 6

# Intelligence feature columns (order matters — shared by store/fetch/coverage).
_INTEL_COLUMNS: Final[tuple[str, ...]] = (
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
    "defi_tvl_7d_change_pct",
    "mvrv_z_score",
    "sopr",
)


def _rows_from_status(status: str) -> int:
    """Parse asyncpg command status tags: 'INSERT 0 3' / 'DELETE 2' / 'UPDATE 1'."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):  # pragma: no cover — defensive
        return 0


# ---------------------------------------------------------------------------
# TimescaleBackend — asyncpg connection pool
# ---------------------------------------------------------------------------


class TimescaleBackend:
    """
    Async TimescaleDB storage backend (GAP-006).

    Lifecycle:
        backend = TimescaleBackend()
        await backend.initialize()
        ...
        await backend.close()

    Or use as an async context manager via open_timescale_storage().
    """

    def __init__(self, dsn: str | None = None) -> None:
        cfg = get_settings().storage
        self._dsn: str = dsn or cfg.timescale_dsn
        self._pool: asyncpg.Pool | None = None
        # Same lazy-lock pattern as StorageBackend (VF-011): asyncio.Lock()
        # at __init__ time would warn when no event loop is running.
        self._lock_init_guard: threading.Lock = threading.Lock()
        self._lock: asyncio.Lock | None = None
        # Do not log credentials — strip anything before '@' in the DSN.
        self._log = log.bind(
            component="storage_timescale",
            db=self._dsn.rsplit("@", 1)[-1],
        )

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
        """Create the asyncpg pool, required directories, and apply DDL + migrations."""
        # Same startup side effect as StorageBackend.initialize() — callers
        # rely on model_dir/log_dir existing after storage init.
        cfg = get_settings().storage
        for p in (cfg.db_path.parent, cfg.model_dir, cfg.log_dir):
            p.mkdir(parents=True, exist_ok=True)

        async with self._get_lock():
            if self._pool is not None:
                return
            pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10)
            try:
                async with pool.acquire() as conn:
                    await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                    await conn.execute(_PG_DDL)
                    for stmt in _HYPERTABLE_SQL:
                        await conn.execute(stmt)
                    await self._run_migrations(conn)
            except BaseException:
                await pool.close()
                raise
            self._pool = pool
            self._log.info("storage.initialized", schema_version=_PG_SCHEMA_VERSION)

    async def _run_migrations(self, conn: asyncpg.Connection) -> None:
        """Apply any pending schema migrations via the schema_version table."""
        current: int = await conn.fetchval("SELECT COALESCE(MAX(version), 0) FROM schema_version")

        if current == _PG_SCHEMA_VERSION:
            return  # Already up to date.

        if current > _PG_SCHEMA_VERSION:
            raise RuntimeError(
                f"DB schema version {current} is AHEAD of code version "
                f"{_PG_SCHEMA_VERSION}. Downgrade not supported — restore from backup."
            )

        self._log.info(
            "storage.migration.start",
            from_version=current,
            to_version=_PG_SCHEMA_VERSION,
        )
        for version, description, sql in _PG_MIGRATIONS:
            if version <= current:
                continue  # Already applied.
            self._log.info(
                "storage.migration.apply",
                version=version,
                description=description,
            )
            async with conn.transaction():
                if sql.strip():
                    await conn.execute(sql)
                await conn.execute("INSERT INTO schema_version (version) VALUES ($1)", version)

        self._log.info(
            "storage.migration.complete",
            schema_version=_PG_SCHEMA_VERSION,
        )

    async def close(self) -> None:
        """Close the connection pool (idempotent)."""
        async with self._get_lock():
            if self._pool is None:
                return
            await self._pool.close()
            self._pool = None
            self._log.info("storage.closed")

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            # SCAN3-011 parity: structured log before raising so the error is
            # queryable in aggregators even when caught broadly upstream.
            log.critical(
                "storage.not_initialized",
                action="call await storage.initialize() before any storage operation",
            )
            raise RuntimeError("TimescaleBackend not initialized — call await backend.initialize()")
        return self._pool

    # ------------------------------------------------------------------
    # Bars
    # ------------------------------------------------------------------

    async def upsert_bars(self, bars: list[BarRecord]) -> int:
        """
        Insert or ignore OHLCV bars.  Returns number of new rows written.
        PRIMARY KEY (symbol, timeframe, ts) prevents duplicates on re-fetch.

        GAP-006: bars ingest is the hot path — a single INSERT..SELECT unnest()
        round-trip instead of executemany.
        """
        if not bars:
            return 0
        pool = self._require_pool()
        cols: tuple[list[Any], ...] = tuple([] for _ in range(10))
        for b in bars:
            for col, val in zip(
                cols,
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
                ),
                strict=True,
            ):
                col.append(val)
        async with pool.acquire() as conn:
            status = await conn.execute(
                """
                INSERT INTO bars
                  (symbol, timeframe, ts, open, high, low, close,
                   volume, quote_volume, taker_buy_vol)
                SELECT * FROM unnest(
                    $1::text[], $2::text[], $3::bigint[],
                    $4::float8[], $5::float8[], $6::float8[], $7::float8[],
                    $8::float8[], $9::float8[], $10::float8[]
                )
                ON CONFLICT (symbol, timeframe, ts) DO NOTHING
                """,
                *cols,
            )
        inserted = _rows_from_status(status)
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
        """
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT symbol, timeframe, ts, open, high, low, close,
                       volume, quote_volume, taker_buy_vol
                FROM bars
                WHERE symbol=$1 AND timeframe=$2 AND ts>=$3
                ORDER BY ts ASC
                LIMIT $4
                """,
                symbol,
                timeframe,
                since_ts,
                limit,
            )
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
            features:   Dict keyed by the intelligence_* column names.
                        Missing keys are stored as NULL.
            confidence: Provider confidence score [0.0, 1.0].
            source:     "backfill" | "live" | "test".
        """
        pool = self._require_pool()
        fetched_at = int(datetime.now(UTC).timestamp() * 1000)

        def _f(key: str) -> float | None:
            v = features.get(key)
            return float(v) if v is not None else None

        values: list[Any] = [symbol, timeframe, bar_ts, fetched_at]
        values.extend(_f(f"intelligence_{c}") for c in _INTEL_COLUMNS)
        values.extend((float(confidence), source))

        col_list = ", ".join(_INTEL_COLUMNS)
        placeholders = ", ".join(f"${i}" for i in range(1, len(values) + 1))
        update_cols = ["fetched_at", *_INTEL_COLUMNS, "confidence", "source"]
        update_set = ", ".join(f"{c}=EXCLUDED.{c}" for c in update_cols)
        # Column names come only from the hardcoded _INTEL_COLUMNS literal —
        # never from external/user input. Values flow through $n placeholders.
        async with pool.acquire() as conn:
            await conn.execute(  # nosemgrep: no-fstring-sql — cols from hardcoded literal
                f"""
                INSERT INTO intelligence_features_history (
                    symbol, timeframe, bar_ts, fetched_at,
                    {col_list},
                    confidence, source
                ) VALUES ({placeholders})
                ON CONFLICT (symbol, timeframe, bar_ts) DO UPDATE SET {update_set}
                """,  # nosec B608
                *values,
            )

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
            DataFrame indexed by bar_ts (Unix ms), columns are the
            intelligence_* feature names + "intelligence_confidence".
        """
        import pandas as _pd

        pool = self._require_pool()
        col_list = ", ".join(_INTEL_COLUMNS)
        async with pool.acquire() as conn:
            rows = await conn.fetch(  # nosemgrep: no-fstring-sql — cols from hardcoded literal
                f"""
                SELECT bar_ts, {col_list}, confidence
                FROM intelligence_features_history
                WHERE symbol=$1 AND timeframe=$2 AND bar_ts>=$3
                ORDER BY bar_ts ASC
                LIMIT $4
                """,  # nosec B608 — col_list is a hardcoded literal
                symbol,
                timeframe,
                since_ts,
                limit,
            )

        if not rows:
            return _pd.DataFrame()

        col_map = {c: f"intelligence_{c}" for c in _INTEL_COLUMNS}
        col_map["confidence"] = "intelligence_confidence"
        return _pd.DataFrame([dict(r) for r in rows]).rename(columns=col_map).set_index("bar_ts")

    async def intelligence_feature_coverage(
        self,
        symbol: str,
        timeframe: str,
    ) -> dict:
        """
        Return per-column non-NULL fraction for intelligence_features_history.

        Returns:
            {
                "total_rows": int,
                "coverage": {"intelligence_<col>": float, ...}   # 0.0-1.0
            }
        """
        pool = self._require_pool()
        count_exprs = ", ".join(f"COUNT({c}) AS {c}" for c in _INTEL_COLUMNS)
        # count_exprs is built only from the hardcoded _INTEL_COLUMNS literal.
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT COUNT(*) AS total, {count_exprs} "  # nosec B608
                "FROM intelligence_features_history WHERE symbol=$1 AND timeframe=$2",
                symbol,
                timeframe,
            )

        data = dict(row) if row is not None else {}
        total = int(data.get("total") or 0)
        if total == 0:
            return {"total_rows": 0, "coverage": {}}

        coverage = {
            f"intelligence_{c}": round(float(data.get(c) or 0) / total, 4) for c in _INTEL_COLUMNS
        }
        return {"total_rows": total, "coverage": coverage}

    async def latest_bar_ts(self, symbol: str, timeframe: str) -> int | None:
        """Return the most recent bar timestamp (Unix ms) or None if no data."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT MAX(ts) FROM bars WHERE symbol=$1 AND timeframe=$2",
                symbol,
                timeframe,
            )
        return int(val) if val is not None else None

    async def latest_close(self, symbol: str, timeframe: str) -> tuple[int, float] | None:
        """
        GAP-005/GAP-015 parity: return (ts, close) for the most recent bar.

        Cheap, single-row read used by the orchestrator each tick.
        Returns None if no bars are stored yet for this symbol/timeframe.
        """
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT ts, close FROM bars
                WHERE symbol=$1 AND timeframe=$2
                ORDER BY ts DESC
                LIMIT 1
                """,
                symbol,
                timeframe,
            )
        if row is None or row["ts"] is None or row["close"] is None:
            return None
        return (int(row["ts"]), float(row["close"]))

    async def bar_count(self, symbol: str, timeframe: str) -> int:
        """Return total stored bar count for a symbol/timeframe."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT COUNT(*) FROM bars WHERE symbol=$1 AND timeframe=$2",
                symbol,
                timeframe,
            )
        return int(val or 0)

    async def prune_old_bars(self, symbol: str, timeframe: str, keep_days: int) -> int:
        """
        Delete bars older than keep_days to cap storage.
        Returns count of deleted rows.
        """
        pool = self._require_pool()
        cutoff_ms = int((datetime.now(tz=UTC).timestamp() - keep_days * 86400) * 1000)
        async with pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM bars WHERE symbol=$1 AND timeframe=$2 AND ts<$3",
                symbol,
                timeframe,
                cutoff_ms,
            )
        deleted = _rows_from_status(status)
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
        pool = self._require_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO trades (
                        id, symbol, timeframe, trading_mode, execution_mode,
                        direction, entry_price, exit_price, quantity, notional_usd,
                        entry_ts, exit_ts, pnl_usd, pnl_pct, fee_usd,
                        kelly_fraction, regime_at_entry, meta_label_prob,
                        exit_reason, approved_by, raw_signal
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                        $12,$13,$14,$15,$16,$17,$18,$19,$20,$21
                    )
                    """,
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
                )
        except asyncpg.UniqueViolationError as exc:
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
        pool = self._require_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE trades
                SET exit_price=$1, exit_ts=$2, pnl_usd=$3, pnl_pct=$4,
                    exit_reason=$5, fee_usd=fee_usd+$6
                WHERE id=$7 AND exit_ts IS NULL
                """,
                # SCAN2-002 parity: fee_usd+$6 accumulates entry fee + exit fee.
                # AND exit_ts IS NULL prevents double-exit writes on the same trade.
                exit_price,
                exit_ts,
                pnl_usd,
                pnl_pct,
                exit_reason,
                fee_usd,
                trade_id,
            )
        if _rows_from_status(status) == 0:
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

        `open_only` restricts the result to trades with no exit recorded —
        the positions the database still believes are live after a crash.
        """
        pool = self._require_pool()
        # Build parameterized query from fixed literal clause fragments only —
        # no user-supplied strings ever reach the SQL text.
        clauses: list[str] = []
        params: list[object] = []
        if symbol is not None:
            params.append(symbol)
            clauses.append(f"symbol=${len(params)}")
        if trading_mode is not None:
            params.append(trading_mode)
            clauses.append(f"trading_mode=${len(params)}")
        if since_ts is not None:
            params.append(since_ts)
            clauses.append(f"entry_ts>=${len(params)}")
        if open_only:
            # No parameter: a literal IS NULL cannot be bound as a value.
            clauses.append("exit_ts IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        params.append(offset)
        query = (
            "SELECT id, symbol, timeframe, trading_mode, execution_mode,"  # nosec B608 — clauses are hardcoded fragments; values use $n placeholders
            " direction, entry_price, exit_price, quantity, notional_usd,"
            " entry_ts, exit_ts, pnl_usd, pnl_pct, fee_usd,"
            " kelly_fraction, regime_at_entry, meta_label_prob,"
            " exit_reason, approved_by, raw_signal"
            f" FROM trades{where}"
            f" ORDER BY entry_ts DESC LIMIT ${len(params) - 1} OFFSET ${len(params)}"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
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
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT pnl_usd FROM trades
                WHERE symbol=$1 AND trading_mode=$2 AND pnl_usd IS NOT NULL
                ORDER BY entry_ts DESC
                LIMIT 20
                """,
                symbol,
                trading_mode,
            )
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
        pool = self._require_pool()
        day_start_ms = int(
            datetime.now(tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            * 1000
        )
        async with pool.acquire() as conn:
            val = await conn.fetchval(
                """
                SELECT COALESCE(SUM(pnl_usd), 0.0) FROM trades
                WHERE symbol=$1 AND trading_mode=$2 AND exit_ts>=$3 AND pnl_usd IS NOT NULL
                """,
                symbol,
                trading_mode,
                day_start_ms,
            )
        return float(val) if val is not None else 0.0

    # ------------------------------------------------------------------
    # Regime snapshots
    # ------------------------------------------------------------------

    async def upsert_regime_snapshot(self, snap: RegimeSnapshotRecord) -> None:
        """Insert or replace regime state at a bar timestamp."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO regime_snapshots
                  (symbol, timeframe, ts, regime_state,
                   prob_ranging, prob_trending, prob_volatile,
                   changepoint_probability, agreement_score)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (symbol, timeframe, ts) DO UPDATE SET
                  regime_state=EXCLUDED.regime_state,
                  prob_ranging=EXCLUDED.prob_ranging,
                  prob_trending=EXCLUDED.prob_trending,
                  prob_volatile=EXCLUDED.prob_volatile,
                  changepoint_probability=EXCLUDED.changepoint_probability,
                  agreement_score=EXCLUDED.agreement_score
                """,
                snap.symbol,
                snap.timeframe,
                snap.ts,
                snap.regime_state,
                snap.prob_ranging,
                snap.prob_trending,
                snap.prob_volatile,
                snap.changepoint_probability,
                snap.agreement_score,
            )

    async def latest_regime(self, symbol: str, timeframe: str) -> RegimeSnapshotRecord | None:
        """Return the most recent regime snapshot or None."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT symbol, timeframe, ts, regime_state,
                       prob_ranging, prob_trending, prob_volatile,
                       changepoint_probability, agreement_score
                FROM regime_snapshots
                WHERE symbol=$1 AND timeframe=$2
                ORDER BY ts DESC LIMIT 1
                """,
                symbol,
                timeframe,
            )
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
        self-tuning scheduler to recover posterior entropy at a historical
        trade's entry time."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT symbol, timeframe, ts, regime_state,
                       prob_ranging, prob_trending, prob_volatile,
                       changepoint_probability, agreement_score
                FROM regime_snapshots
                WHERE symbol=$1 AND timeframe=$2 AND ts<=$3
                ORDER BY ts DESC LIMIT 1
                """,
                symbol,
                timeframe,
                ts,
            )
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
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT symbol, timeframe, ts, open, high, low, close,
                       volume, quote_volume, taker_buy_vol
                FROM (
                    SELECT symbol, timeframe, ts, open, high, low, close,
                           volume, quote_volume, taker_buy_vol
                    FROM bars
                    WHERE symbol=$1 AND timeframe=$2 AND ts<=$3
                    ORDER BY ts DESC LIMIT $4
                ) sub
                ORDER BY ts ASC
                """,
                symbol,
                timeframe,
                ts,
                limit,
            )
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
        """Best-effort log of a tradeable signal that never opened a position."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO missed_trades (
                    id, symbol, timeframe, direction, reason,
                    kelly_fraction, meta_label_prob, raw_signal,
                    regime_at_entry, notional_usd, ts
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (id) DO NOTHING
                """,
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
            )

    async def fetch_missed_trades(
        self, symbol: str | None = None, limit: int = 50
    ) -> list[MissedTradeRecord]:
        """Most recent missed trades, newest first."""
        pool = self._require_pool()
        base_query = (
            "SELECT id, symbol, timeframe, direction, reason,"
            " kelly_fraction, meta_label_prob, raw_signal, regime_at_entry,"
            " notional_usd, ts FROM missed_trades"
        )
        params: list[object] = []
        if symbol is not None:
            base_query += " WHERE symbol=$1"
            params.append(symbol)
        base_query += f" ORDER BY ts DESC LIMIT ${len(params) + 1}"
        params.append(limit)
        async with pool.acquire() as conn:
            rows = await conn.fetch(base_query, *params)
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
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO model_metrics
                  (model_name, timeframe, version, oos_sharpe, max_drawdown,
                   n_trades, accuracy, precision_score, recall_score,
                   f1_score, live_gate_pass)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (model_name, timeframe, version) DO UPDATE SET
                  oos_sharpe=EXCLUDED.oos_sharpe,
                  max_drawdown=EXCLUDED.max_drawdown,
                  n_trades=EXCLUDED.n_trades,
                  accuracy=EXCLUDED.accuracy,
                  precision_score=EXCLUDED.precision_score,
                  recall_score=EXCLUDED.recall_score,
                  f1_score=EXCLUDED.f1_score,
                  live_gate_pass=EXCLUDED.live_gate_pass
                """,
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
            )
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
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT model_name, timeframe, version, oos_sharpe, max_drawdown,
                       n_trades, accuracy, precision_score, recall_score,
                       f1_score, live_gate_pass
                FROM model_metrics
                WHERE model_name=$1 AND timeframe=$2
                ORDER BY created_at DESC LIMIT 1
                """,
                model_name,
                timeframe,
            )
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
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO equity_curve
                  (ts, trading_mode, equity_usd, cash_usd, unrealized_pnl,
                   daily_pnl_usd, daily_pnl_pct, peak_equity_usd, drawdown_pct)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (ts, trading_mode) DO UPDATE SET
                  equity_usd=EXCLUDED.equity_usd,
                  cash_usd=EXCLUDED.cash_usd,
                  unrealized_pnl=EXCLUDED.unrealized_pnl,
                  daily_pnl_usd=EXCLUDED.daily_pnl_usd,
                  daily_pnl_pct=EXCLUDED.daily_pnl_pct,
                  peak_equity_usd=EXCLUDED.peak_equity_usd,
                  drawdown_pct=EXCLUDED.drawdown_pct
                """,
                record.ts,
                record.trading_mode,
                record.equity_usd,
                record.cash_usd,
                record.unrealized_pnl,
                record.daily_pnl_usd,
                record.daily_pnl_pct,
                record.peak_equity_usd,
                record.drawdown_pct,
            )

    async def fetch_equity_curve(
        self,
        trading_mode: str,
        since_ts: int | None = None,
        limit: int = 1440,
    ) -> list[EquityRecord]:
        """Return equity snapshots in ascending time order."""
        pool = self._require_pool()
        # Safe clause pattern (C-06 parity): only hardcoded literal fragments
        # ever reach the SQL text; values flow through $n placeholders.
        if since_ts is not None:
            query = (
                "SELECT ts, trading_mode, equity_usd, cash_usd, unrealized_pnl,"
                " daily_pnl_usd, daily_pnl_pct, peak_equity_usd, drawdown_pct"
                " FROM equity_curve WHERE trading_mode=$1 AND ts>=$2"
                " ORDER BY ts ASC LIMIT $3"
            )
            params: list[object] = [trading_mode, since_ts, limit]
        else:
            query = (
                "SELECT ts, trading_mode, equity_usd, cash_usd, unrealized_pnl,"
                " daily_pnl_usd, daily_pnl_pct, peak_equity_usd, drawdown_pct"
                " FROM equity_curve WHERE trading_mode=$1"
                " ORDER BY ts ASC LIMIT $2"
            )
            params = [trading_mode, limit]
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
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
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT ts, trading_mode, equity_usd, cash_usd, unrealized_pnl,
                       daily_pnl_usd, daily_pnl_pct, peak_equity_usd, drawdown_pct
                FROM equity_curve
                WHERE trading_mode=$1
                ORDER BY ts DESC LIMIT 1
                """,
                trading_mode,
            )
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
        pool = self._require_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT MIN(ts) FROM equity_curve WHERE trading_mode=$1",
                trading_mode,
            )
        return int(val) if val is not None else None

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
        pool = self._require_pool()
        async with pool.acquire() as conn:
            val = await conn.fetchval(
                "SELECT COUNT(*) FROM bars WHERE symbol=$1",
                symbol,
            )
        if not val:
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
        pool = self._require_pool()
        details_json = json.dumps(details or {})
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit_log (event_type, operator, details)
                VALUES ($1, $2, $3)
                """,
                event_type,
                operator,
                details_json,
            )
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
        pool = self._require_pool()
        counts: dict[str, object] = {}
        # VF-009 parity: defence-in-depth character-level validation of each
        # allowlisted table name before interpolating into the query text.
        _safe_table_re = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
        async with pool.acquire() as conn:
            for table in _ALLOWED_TABLES:
                if not _safe_table_re.match(table):
                    raise RuntimeError(f"health_check: table {table!r} contains unsafe characters")
                val = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")  # nosec B608
                counts[table] = int(val or 0)
        return counts


# ---------------------------------------------------------------------------
# Async context manager — preferred lifecycle management
# ---------------------------------------------------------------------------


@asynccontextmanager
async def open_timescale_storage(dsn: str | None = None) -> AsyncIterator[TimescaleBackend]:
    """
    Async context manager for TimescaleBackend (GAP-006).

    Usage::

        async with open_timescale_storage() as storage:
            bars = await storage.fetch_bars("BTC/USDT", "15m", since_ts)
    """
    backend = TimescaleBackend(dsn=dsn)
    await backend.initialize()
    try:
        yield backend
    finally:
        await backend.close()
