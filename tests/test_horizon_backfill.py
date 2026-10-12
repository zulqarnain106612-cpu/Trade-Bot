"""Causal and isolated historical warm-start regression tests.

Decides:
  - DATA-006 — historical horizon warm-start is causal and leaves source storage unchanged
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.models.horizon_backfill import warm_start


def test_warm_start_is_causal_isolated_and_resumable(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE bars (
                symbol TEXT, timeframe TEXT, ts INTEGER, open REAL, high REAL,
                low REAL, close REAL, volume REAL
            )"""
        )
        connection.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "BTC/USDT",
                    "1m",
                    1_700_000_000_000 + i * 60_000,
                    100 + i,
                    101 + i,
                    99 + i,
                    100 + i,
                    1.0,
                )
                for i in range(4)
            ],
        )

    artifact_root = tmp_path / "isolated-candidate"
    horizons = [
        {"id": 0, "seconds": 30, "models": ["mlp"]},
        {"id": 1, "seconds": 120, "models": ["mlp"]},
    ]
    result = warm_start(
        database,
        artifact_root,
        symbol="BTC/USDT",
        timeframe="1m",
        horizons=horizons,
        page_size=2,
        update_batch_size=2,
    )

    assert result["events_ingested_or_replayed"] == 4
    assert result["horizons"][1]["training_samples"] == 2
    assert 0 not in result["horizons"]  # no 30-second labels from 1-minute bars
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 4
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='horizon_samples'"
            ).fetchone()[0]
            == 0
        )

    resumed = warm_start(
        database,
        artifact_root,
        symbol="BTC/USDT",
        timeframe="1m",
        horizons=horizons,
        page_size=2,
        update_batch_size=2,
        resume=True,
    )
    assert resumed["horizons"][1]["training_samples"] == 2
