"""Causal and isolated historical warm-start regression tests.

Decides:
  - DATA-006 — historical horizon warm-start is causal and leaves source storage unchanged
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.models.horizon_backfill import iter_market_bars, warm_start


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


def test_iter_market_bars_rejects_missing_database_and_missing_table(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list(iter_market_bars(tmp_path / "missing.db", symbol="BTC/USDT", timeframe="1m"))
    database = tmp_path / "empty.db"
    sqlite3.connect(database).close()
    with pytest.raises(ValueError, match="no bars table"):
        list(iter_market_bars(database, symbol="BTC/USDT", timeframe="1m"))


def test_iter_market_bars_filters_invalid_rows_and_clamps_page_size(tmp_path: Path) -> None:
    database = tmp_path / "bars.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE bars (symbol TEXT, timeframe TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        connection.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("BTC/USDT", "1m", 1, 1, 2, 0, 1, 1),
                ("BTC/USDT", "1m", 2, 1, 2, 1, float("inf"), 1),
                ("BTC/USDT", "1m", 3, 1, 2, 1, 1.5, -1),
                ("BTC/USDT", "1m", 4, 1, 2, 1, 1.5, 0),
            ],
        )
    rows = list(iter_market_bars(database, symbol="BTC/USDT", timeframe="1m", page_size=0))
    assert len(rows) == 1
    assert rows[0]["timestamp_ms"] == 4.0
    assert rows[0]["volume"] == 0.0


def test_warm_start_rejects_unsafe_roots_timeframes_and_resume_state(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "bars.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE bars (symbol TEXT, timeframe TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
    horizons = [{"id": 1, "seconds": 120, "models": ["mlp"]}]
    repo_root = Path.cwd()
    with pytest.raises(ValueError, match="outside the repository"):
        warm_start(
            database,
            repo_root / ".tmp-warm-start",
            symbol="BTC/USDT",
            timeframe="1m",
            horizons=horizons,
        )
    with pytest.raises(ValueError, match="unsupported"):
        warm_start(
            database, tmp_path / "unsupported", symbol="BTC/USDT", timeframe="5m", horizons=horizons
        )
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "unrelated.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(FileExistsError, match="resume=True"):
        warm_start(database, occupied, symbol="BTC/USDT", timeframe="1m", horizons=horizons)
    with pytest.raises(ValueError, match="does not contain"):
        warm_start(
            database, occupied, symbol="BTC/USDT", timeframe="1m", horizons=horizons, resume=True
        )


def test_warm_start_counts_invalid_events_and_returns_training_summary(tmp_path, monkeypatch):
    import src.models.horizon_backfill as backfill

    bars = [
        {
            "timestamp_ms": 1_700_000_000_000.0,
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "volume": 2.0,
        },
        {
            "timestamp_ms": 1_700_000_060_000.0,
            "open": 101.0,
            "high": 103.0,
            "low": 100.0,
            "close": 102.0,
            "volume": 3.0,
        },
    ]

    class Store:
        def __init__(self, root):
            self.root = root
            self.seen = 0
            self.training_calls = 0

        def observe_event(self, **kwargs):
            self.seen += 1
            if self.seen == 1:
                raise ValueError("bad persisted row")

        def train_pending(self, **kwargs):
            self.training_calls += 1
            return {"attempted": 0, "updated": 0, "rejected": 0}

        def stats(self, **kwargs):
            return {0: {"matured_samples": 1}}

    monkeypatch.setattr(backfill, "iter_market_bars", lambda *args, **kwargs: iter(bars))
    monkeypatch.setattr(backfill, "HorizonLearningStore", Store)
    result = backfill.warm_start(
        tmp_path / "source.db",
        tmp_path / "isolated-models",
        symbol="BTC/USDT",
        timeframe="1m",
        horizons=[{"id": 0, "seconds": 60, "models": ["mlp"]}],
    )
    assert result["events_ingested_or_replayed"] == 1
    assert result["invalid_rows"] == 1
    assert result["horizons"] == {0: {"matured_samples": 1}}
    assert result["activation"].startswith("not requested")


def test_backfill_cli_serializes_the_warm_start_result(tmp_path, monkeypatch, capsys):
    import json
    import sys

    import src.models.horizon_backfill as backfill
    import src.models.horizon_config as horizon_config

    source = tmp_path / "source.db"
    root = tmp_path / "isolated-models"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "horizon_backfill",
            "--database",
            str(source),
            "--artifact-root",
            str(root),
            "--symbol",
            "ETH/USDT",
            "--timeframe",
            "15m",
            "--page-size",
            "10",
            "--update-batch-size",
            "4",
            "--resume",
        ],
    )
    monkeypatch.setattr(horizon_config, "load_horizon_specs", lambda: {"short": {"id": 0}})
    monkeypatch.setattr(
        backfill,
        "warm_start",
        lambda *args, **kwargs: {"symbol": kwargs["symbol"], "resume": kwargs["resume"]},
    )

    backfill.main()

    assert json.loads(capsys.readouterr().out) == {"symbol": "ETH/USDT", "resume": True}
