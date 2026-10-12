"""Bounded, read-only OHLCV warm-start into an isolated horizon artifact root."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import deque
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

from src.models.horizon_learning import HorizonLearningStore


def iter_market_bars(
    database: Path,
    *,
    symbol: str,
    timeframe: str,
    page_size: int = 500,
) -> Iterator[dict[str, float]]:
    """Read persisted bars in ascending keyset pages without modifying the DB."""
    if not database.is_file():
        raise FileNotFoundError(database)
    page_size = max(1, min(int(page_size), 5000))
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "bars" not in tables:
            raise ValueError("source database has no bars table")
        last_ts: int | None = None
        while True:
            if last_ts is None:
                query = (
                    "SELECT ts, open, high, low, close, volume FROM bars "
                    "WHERE symbol=? AND timeframe=? ORDER BY ts LIMIT ?"
                )
                params: tuple[Any, ...] = (symbol, timeframe, page_size)
            else:
                query = (
                    "SELECT ts, open, high, low, close, volume FROM bars "
                    "WHERE symbol=? AND timeframe=? AND ts>? ORDER BY ts LIMIT ?"
                )
                params = (symbol, timeframe, last_ts, page_size)
            rows = connection.execute(query, params).fetchall()
            if not rows:
                break
            for timestamp, open_, high, low, close, volume in rows:
                last_ts = int(timestamp)
                numeric = tuple(float(value) for value in (open_, high, low, close, volume))
                if not all(math.isfinite(value) for value in numeric):
                    continue
                if min(numeric[:4]) <= 0 or numeric[4] < 0:
                    continue
                yield {
                    "timestamp_ms": float(last_ts),
                    "open": numeric[0],
                    "high": numeric[1],
                    "low": numeric[2],
                    "close": numeric[3],
                    "volume": numeric[4],
                }
    finally:
        connection.close()


def warm_start(
    database: Path,
    artifact_root: Path,
    *,
    symbol: str,
    timeframe: str,
    horizons: list[dict[str, Any]],
    page_size: int = 500,
    update_batch_size: int = 100,
    resume: bool = False,
) -> dict[str, Any]:
    """Replay OHLCV chronologically into a separate, non-production model root."""
    root = artifact_root.resolve()
    repo_root = Path.cwd().resolve()
    with suppress(ValueError):
        root.relative_to(repo_root)
    if root.is_relative_to(repo_root):
        raise ValueError("warm-start artifact root must be outside the repository")
    learning_db = root / "horizon_learning.sqlite3"
    if root.exists() and any(root.iterdir()):
        if not resume:
            raise FileExistsError(f"use resume=True to continue this isolated warm-start: {root}")
        if not learning_db.is_file():
            raise ValueError("resume root does not contain a horizon learning database")

    cadence_by_timeframe = {"1m": 60.0, "15m": 900.0, "4h": 14400.0}
    nominal_cadence = cadence_by_timeframe.get(timeframe.lower())
    if nominal_cadence is None:
        raise ValueError(f"unsupported persisted source timeframe: {timeframe}")

    store = HorizonLearningStore(root)
    gaps: deque[float] = deque(maxlen=64)
    previous_ts: float | None = None
    ingested = invalid = 0
    for bar in iter_market_bars(database, symbol=symbol, timeframe=timeframe, page_size=page_size):
        ts = bar["timestamp_ms"] / 1000.0
        if previous_ts is not None and ts > previous_ts:
            gaps.append(ts - previous_ts)
        previous_ts = ts
        observed = statistics.median(gaps) if len(gaps) >= 5 else None
        cadence = max(nominal_cadence, observed or nominal_cadence)
        close = bar["close"]
        try:
            store.observe_event(
                event_id=f"{symbol}:{timeframe}:{int(bar['timestamp_ms'])}",
                symbol=symbol,
                event_ts=ts,
                close=close,
                features={"price": close, "volume": bar["volume"]},
                horizons=horizons,
                model_root=root,
                provider="persisted_market_bars",
                quality="historical_ohlcv_only",
                event_payload={**bar, "source_timeframe": timeframe},
                defer_training=True,
                source_cadence_seconds=cadence,
                source_timeframe=timeframe,
            )
            ingested += 1
        except ValueError:
            invalid += 1

    update_totals = {"updated": 0, "rejected": 0}
    while True:
        batch = store.train_pending(
            symbol=symbol,
            horizons=horizons,
            model_root=root,
            limit=update_batch_size,
            source_timeframe=timeframe,
        )
        update_totals["updated"] += batch["updated"]
        update_totals["rejected"] += batch["rejected"]
        if batch["attempted"] == 0 or (batch["updated"] == 0 and batch["rejected"] > 0):
            break

    stats = store.stats(symbol=symbol, source_timeframe=timeframe)
    return {
        "source_database": str(database.resolve()),
        "artifact_root": str(root),
        "symbol": symbol,
        "timeframe": timeframe,
        "source_cadence_seconds": cadence if previous_ts is not None else None,
        "events_ingested_or_replayed": ingested,
        "invalid_rows": invalid,
        "training_updates": update_totals["updated"],
        "training_rejections": update_totals["rejected"],
        "horizons": stats,
        "activation": "not requested; artifacts remain isolated candidates",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="1m", choices=("1m", "15m", "4h"))
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--update-batch-size", type=int, default=100)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    from src.models.horizon_config import load_horizon_specs

    result = warm_start(
        args.database,
        args.artifact_root,
        symbol=args.symbol,
        timeframe=args.timeframe,
        horizons=list(load_horizon_specs().values()),
        page_size=args.page_size,
        update_batch_size=args.update_batch_size,
        resume=args.resume,
    )
    sys.stdout.write(json.dumps(result, sort_keys=True, default=str) + "\n")


if __name__ == "__main__":
    main()
