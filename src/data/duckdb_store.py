"""
DuckDB OLAP store — backtests, feature snapshots, horizon performance analytics.

Runs entirely in-process (no server needed); the file path is configurable via
DUCKDB_PATH env var (default: ./data/crypto_intel.duckdb).

Tables:
  ohlcv_snapshots  — OHLCV bars per symbol/timeframe for backtest replay
  horizon_metrics  — per-horizon Sharpe, confidence, drift flags
  ecc_signals      — ECC feature vectors per evaluation cycle
  feature_log      — full feature matrix per bar for post-trade attribution
"""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_DB_PATH = Path(os.environ.get("DUCKDB_PATH", "data/crypto_intel.duckdb"))

_DDL = """
CREATE TABLE IF NOT EXISTS ohlcv_snapshots (
    ts          BIGINT NOT NULL,
    symbol      VARCHAR NOT NULL,
    timeframe   VARCHAR NOT NULL,
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,
    volume      DOUBLE,
    PRIMARY KEY (ts, symbol, timeframe)
);

CREATE TABLE IF NOT EXISTS horizon_metrics (
    ts              BIGINT NOT NULL,
    horizon_id      INTEGER NOT NULL,
    label           VARCHAR,
    sharpe          DOUBLE,
    confidence      DOUBLE,
    direction       INTEGER,
    drift_detected  BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (ts, horizon_id)
);

CREATE TABLE IF NOT EXISTS ecc_signals (
    ts                  BIGINT NOT NULL PRIMARY KEY,
    cluster_flow_score  DOUBLE,
    ecdsa_weakness      DOUBLE,
    schnorr_divergence  DOUBLE,
    hodler_index        DOUBLE,
    dark_pool_pressure  DOUBLE,
    ecc_anomaly         DOUBLE
);

CREATE TABLE IF NOT EXISTS feature_log (
    ts          BIGINT NOT NULL,
    symbol      VARCHAR NOT NULL,
    feature_key VARCHAR NOT NULL,
    value       DOUBLE,
    PRIMARY KEY (ts, symbol, feature_key)
);
"""


class DuckDBStore:
    """Thread-safe in-process DuckDB OLAP store."""

    def __init__(self, path: Path | None = None) -> None:
        import duckdb

        self._path = path or _DB_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self._path))
        self._conn.execute(_DDL)
        log.info("duckdb_store_init", path=str(self._path))

    @contextmanager
    def _tx(self) -> Generator[Any, None, None]:
        self._conn.begin()
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def write_ohlcv(self, rows: list[dict]) -> None:
        if not rows:
            return
        _df = pd.DataFrame(rows)
        with self._tx() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO ohlcv_snapshots
                    (ts, symbol, timeframe, open, high, low, close, volume)
                SELECT ts, symbol, timeframe, open, high, low, close, volume FROM _df
                """
            )

    def write_horizon_metric(
        self,
        horizon_id: int,
        label: str,
        sharpe: float,
        confidence: float,
        direction: int,
        drift_detected: bool = False,
    ) -> None:
        ts = int(datetime.now(UTC).timestamp() * 1000)
        with self._tx() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO horizon_metrics
                    (ts, horizon_id, label, sharpe, confidence, direction, drift_detected)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [ts, horizon_id, label, sharpe, confidence, direction, drift_detected],
            )

    def write_ecc_signal(self, signals: dict[str, float]) -> None:
        ts = int(datetime.now(UTC).timestamp() * 1000)
        with self._tx() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO ecc_signals
                    (ts, cluster_flow_score, ecdsa_weakness, schnorr_divergence,
                     hodler_index, dark_pool_pressure, ecc_anomaly)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ts,
                    signals.get("cluster_flow_score", 0.0),
                    signals.get("ecdsa_weakness", 0.0),
                    signals.get("schnorr_divergence", 0.0),
                    signals.get("hodler_index", 0.0),
                    signals.get("dark_pool_pressure", 0.0),
                    signals.get("ecc_anomaly", 0.0),
                ],
            )

    def write_feature_log(self, symbol: str, features: dict[str, float]) -> None:
        ts = int(datetime.now(UTC).timestamp() * 1000)
        rows = [
            {"ts": ts, "symbol": symbol, "feature_key": k, "value": float(v)}
            for k, v in features.items()
        ]
        if not rows:
            return
        _df = pd.DataFrame(rows)
        with self._tx() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO feature_log (ts, symbol, feature_key, value)
                SELECT ts, symbol, feature_key, value FROM _df
                """
            )

    def query_horizon_history(self, horizon_id: int, limit: int = 500) -> pd.DataFrame:
        return self._conn.execute(
            "SELECT * FROM horizon_metrics WHERE horizon_id=? ORDER BY ts DESC LIMIT ?",
            [horizon_id, limit],
        ).df()

    def query_ohlcv(self, symbol: str, timeframe: str, limit: int = 2000) -> pd.DataFrame:
        return self._conn.execute(
            """SELECT * FROM ohlcv_snapshots
               WHERE symbol=? AND timeframe=?
               ORDER BY ts DESC LIMIT ?""",
            [symbol, timeframe, limit],
        ).df()

    def query_ecc_history(self, limit: int = 1000) -> pd.DataFrame:
        return self._conn.execute(
            "SELECT * FROM ecc_signals ORDER BY ts DESC LIMIT ?", [limit]
        ).df()

    def close(self) -> None:
        self._conn.close()
        log.info("duckdb_store_closed")
