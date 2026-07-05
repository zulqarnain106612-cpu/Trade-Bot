"""
Comprehensive coverage tests for src/engine/orchestrator.py.

Coverage target: 10% → 70%+. Every major code path exercised with mocked
external dependencies — no real network, DB, or model I/O.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Timeframe, TradingMode


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_storage():
    s = AsyncMock()
    s.health_check = AsyncMock(return_value=True)
    s.latest_model_metrics = AsyncMock(return_value=None)
    s.fetch_trades = AsyncMock(return_value=[])
    s.earliest_equity_ts = AsyncMock(return_value=None)
    s.insert_bar = AsyncMock(return_value=None)
    s.fetch_bars = AsyncMock(return_value=[])
    s.initialize = AsyncMock(return_value=None)
    s.close = AsyncMock(return_value=None)
    return s


def _make_fetcher():
    f = AsyncMock()
    f.bootstrap_history = AsyncMock(return_value=100)
    f.fetch_ohlcv = AsyncMock(return_value=[])
    return f


def _make_executor():
    e = AsyncMock()
    e.initialize = AsyncMock(return_value=None)
    e.get_daily_pnl = AsyncMock(return_value=0.0)
    e.get_consecutive_losses = AsyncMock(return_value=0)
    e.equity_usd = 1000.0
    e.starting_equity_usd = 1000.0
    e.shutdown = AsyncMock(return_value=None)
    return e


def _selftest_ok():
    return {"passed": True, "n_rows": 50}


def _selftest_fail():
    return {"passed": False, "error": "pipeline broken"}


# ---------------------------------------------------------------------------
# Context manager that patches all external dependencies for Orchestrator
# ---------------------------------------------------------------------------

from contextlib import contextmanager


@contextmanager
def _orch_patches(executor=None, selftest=None, load_models=True):
    """Patch everything Orchestrator touches externally."""
    if executor is None:
        executor = _make_executor()
    if selftest is None:
        selftest = _selftest_ok
    mon = MagicMock()
    mon.register_probe = MagicMock()
    mon.register_tick_source = MagicMock()
    mon.start = AsyncMock(return_value=None)
    mon.stop = AsyncMock(return_value=None)

    trainer = MagicMock()
    trainer.train = AsyncMock(return_value=None)
    trainer.is_trained = True
    trainer.train_sharpe = 2.0
    trainer.oos_sharpe = 1.5
    trainer.train_accuracy = 0.62
    trainer.oos_accuracy = 0.59
    trainer.train_win_rate = 0.55
    trainer.max_drawdown_pct = 0.08
    trainer.trades_count = 400

    detector = MagicMock()
    detector.fit = MagicMock()
    detector.is_fitted = MagicMock(return_value=True)

    direction_model = MagicMock()
    meta_model = MagicMock()

    with (
        patch("src.engine.orchestrator.PaperExecutor", return_value=executor),
        patch("src.engine.orchestrator.LiveExecutor", return_value=executor),
        patch("src.engine.orchestrator.get_monitor", return_value=mon),
        patch("src.engine.orchestrator.run_pipeline_selftest", side_effect=selftest),
        patch("src.engine.orchestrator.ModelTrainer", return_value=trainer),
        patch("src.engine.orchestrator.ModelTrainer.load_direction",
              return_value=direction_model),
        patch("src.engine.orchestrator.ModelTrainer.load_meta",
              return_value=meta_model),
        patch("src.engine.orchestrator.RegimeDetector", return_value=detector),
        patch("src.engine.orchestrator.SignalEngine"),
    ):
        yield executor, trainer, detector

