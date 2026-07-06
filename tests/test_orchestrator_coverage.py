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
from src.data.storage import BarRecord


def _make_bars(n: int = 350) -> list[BarRecord]:
    """Return minimal BarRecord list large enough to pass the 300-row guard."""
    return [
        BarRecord(
            ts=1_700_000_000_000 + i * 900_000,
            symbol="BTC/USDT",
            timeframe="15m",
            open=40_000.0 + i,
            high=40_100.0 + i,
            low=39_900.0 + i,
            close=40_050.0 + i,
            volume=10.0 + i,
            quote_volume=400_500.0,
        )
        for i in range(n)
    ]


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
    s.fetch_bars = AsyncMock(return_value=_make_bars())   # ≥300 rows for trainer
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


# ---------------------------------------------------------------------------
# Tests: __init__ and basic state
# ---------------------------------------------------------------------------

class TestOrchestratorInit:
    def test_init_sets_symbol_and_timeframes(self):
        from src.engine.orchestrator import Orchestrator
        storage = _make_storage()
        fetcher = _make_fetcher()
        with patch("src.engine.orchestrator.get_settings") as mock_cfg:
            cfg = MagicMock()
            cfg.primary_symbol = "BTC/USDT"
            cfg.active_timeframes = [Timeframe.INTRADAY]
            cfg.primary_timeframe = Timeframe.INTRADAY
            cfg.trading_mode = TradingMode.PAPER
            cfg.starting_capital_usd = 1000.0
            mock_cfg.return_value = cfg
            orch = Orchestrator(storage, fetcher)
        assert orch._symbol == "BTC/USDT"
        assert orch._running is False
        assert orch._executor is None
        assert orch._engines == {}

    def test_init_creates_stop_event(self):
        from src.engine.orchestrator import Orchestrator
        storage = _make_storage()
        fetcher = _make_fetcher()
        with patch("src.engine.orchestrator.get_settings") as mock_cfg:
            cfg = MagicMock()
            cfg.primary_symbol = "ETH/USDT"
            cfg.active_timeframes = [Timeframe.INTRADAY]
            cfg.primary_timeframe = Timeframe.INTRADAY
            cfg.trading_mode = TradingMode.PAPER
            cfg.starting_capital_usd = 500.0
            mock_cfg.return_value = cfg
            orch = Orchestrator(storage, fetcher)
        assert isinstance(orch._stop_event, asyncio.Event)
        assert not orch._stop_event.is_set()



# ---------------------------------------------------------------------------
# Helper to build a configured Orchestrator inside all required patches
# ---------------------------------------------------------------------------

def _make_orch(storage=None, fetcher=None):
    from src.engine.orchestrator import Orchestrator
    if storage is None:
        storage = _make_storage()
    if fetcher is None:
        fetcher = _make_fetcher()
    with patch("src.engine.orchestrator.get_settings") as mock_cfg:
        cfg = MagicMock()
        cfg.primary_symbol = "BTC/USDT"
        cfg.active_timeframes = [Timeframe.INTRADAY]
        cfg.primary_timeframe = Timeframe.INTRADAY
        cfg.trading_mode = TradingMode.PAPER
        cfg.starting_capital_usd = 1000.0
        cfg.storage.model_dir = "/tmp/models"
        mock_cfg.return_value = cfg
        orch = Orchestrator(storage, fetcher)
    return orch


# ---------------------------------------------------------------------------
# Tests: startup — happy path
# ---------------------------------------------------------------------------

class TestOrchestratorStartup:
    @pytest.mark.asyncio
    async def test_startup_paper_creates_executor(self):
        storage = _make_storage()
        fetcher = _make_fetcher()
        orch = _make_orch(storage, fetcher)
        with _orch_patches() as (executor, _, __):
            await orch.startup()
        assert orch._executor is executor

    @pytest.mark.asyncio
    async def test_startup_bootstrap_failure_raises(self):
        storage = _make_storage()
        fetcher = _make_fetcher()
        orch = _make_orch(storage, fetcher)
        with _orch_patches() as (executor, _, __):
            executor.initialize = AsyncMock(return_value=None)
            with patch("src.engine.orchestrator.PaperExecutor", return_value=executor):
                # Make bootstrap fail
                fetcher.bootstrap_history = AsyncMock(
                    side_effect=RuntimeError("exchange down")
                )
                with pytest.raises(RuntimeError, match="Bootstrap failed"):
                    await orch.startup()

    @pytest.mark.asyncio
    async def test_startup_selftest_failure_raises(self):
        storage = _make_storage()
        fetcher = _make_fetcher()
        orch = _make_orch(storage, fetcher)
        with _orch_patches(selftest=_selftest_fail):
            with pytest.raises(RuntimeError, match="self-test FAILED"):
                await orch.startup()

    @pytest.mark.asyncio
    async def test_startup_engines_populated(self):
        storage = _make_storage()
        fetcher = _make_fetcher()
        orch = _make_orch(storage, fetcher)
        mock_engine = MagicMock()
        with _orch_patches() as (_, __, ___):
            with patch("src.engine.orchestrator.SignalEngine",
                       return_value=mock_engine):
                await orch.startup()
        assert Timeframe.INTRADAY.value in orch._engines



# ---------------------------------------------------------------------------
# Tests: stop / shutdown
# ---------------------------------------------------------------------------

class TestOrchestratorStopShutdown:
    def test_stop_sets_event(self):
        orch = _make_orch()
        orch._stop_event = asyncio.Event()
        orch.stop()
        assert orch._stop_event.is_set()

    @pytest.mark.asyncio
    async def test_shutdown_calls_executor(self):
        orch = _make_orch()
        executor = _make_executor()
        orch._executor = executor
        mon = MagicMock()
        mon.stop = AsyncMock(return_value=None)
        with patch("src.engine.orchestrator.get_monitor", return_value=mon):
            await orch.shutdown()
        executor.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown_no_executor_does_not_raise(self):
        orch = _make_orch()
        orch._executor = None
        mon = MagicMock()
        mon.stop = AsyncMock(return_value=None)
        with patch("src.engine.orchestrator.get_monitor", return_value=mon):
            await orch.shutdown()  # must not raise


# ---------------------------------------------------------------------------
# Tests: _tick — signal engine dispatch
# ---------------------------------------------------------------------------

class TestOrchestratorTick:
    @pytest.mark.asyncio
    async def test_tick_skips_when_no_engine(self):
        orch = _make_orch()
        orch._engines = {}
        orch._executor = _make_executor()
        # Should return without error
        await orch._tick(Timeframe.INTRADAY)

    @pytest.mark.asyncio
    async def test_tick_skips_when_no_executor(self):
        orch = _make_orch()
        mock_engine = MagicMock()
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}
        orch._executor = None
        await orch._tick(Timeframe.INTRADAY)

    @pytest.mark.asyncio
    async def test_tick_increments_count(self):
        from src.engine.signal_engine import SignalResult
        orch = _make_orch()
        executor = _make_executor()
        orch._executor = executor

        skip_result = SignalResult(
            tradeable=False, direction=0, p_long=0.3, p_bet=0.3,
            kelly_result=None, regime=None, gate_result=None,
            skip_reason="test",
        )
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=skip_result)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}
        orch._tick_counts = {Timeframe.INTRADAY.value: 0}
        orch._last_tick_ts = {Timeframe.INTRADAY.value: 0.0}

        storage = _make_storage()
        orch._storage = storage

        with patch("src.engine.orchestrator.compute_win_loss_stats",
                   return_value=(0, 0.0, 0.0)):
            await orch._tick(Timeframe.INTRADAY)

        assert orch._tick_counts[Timeframe.INTRADAY.value] == 1



    @pytest.mark.asyncio
    async def test_tick_tradeable_calls_executor(self):
        from src.engine.signal_engine import SignalResult
        from src.risk.kelly import KellyResult
        orch = _make_orch()
        executor = _make_executor()
        executor.submit_signal = AsyncMock(return_value=("trade-123", "opened"))
        executor.get_current_equity = AsyncMock(return_value=1000.0)
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor
        orch._storage = _make_storage()
        orch._storage.latest_close = AsyncMock(return_value=None)

        kr = KellyResult(adjusted_fraction=0.05, entry_price=42000.0,
                         quantity=0.001, notional_usd=42.0, is_capped=False)
        tradeable = SignalResult(
            tradeable=True, direction=1, p_long=0.75, p_bet=0.7,
            kelly_result=kr, regime=None, gate_result=None, skip_reason=None,
        )
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=tradeable)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}
        orch._tick_counts = {Timeframe.INTRADAY.value: 0}
        orch._last_tick_ts = {Timeframe.INTRADAY.value: 0.0}

        with (
            patch("src.engine.orchestrator.compute_win_loss_stats",
                  return_value=(0, 0.0, 0.0)),
            patch("src.engine.orchestrator.update_metrics"),
        ):
            await orch._tick(Timeframe.INTRADAY)

        executor.submit_signal.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: _train_models
# ---------------------------------------------------------------------------

class TestOrchestratorTrainModels:
    def _orch_for_train(self):
        from src.engine.orchestrator import Orchestrator
        storage = _make_storage()
        storage.insert_model_metrics = AsyncMock(return_value=None)
        fetcher = _make_fetcher()
        with patch("src.engine.orchestrator.get_settings") as mock_cfg:
            cfg = MagicMock()
            cfg.primary_symbol = "BTC/USDT"
            cfg.active_timeframes = [Timeframe.INTRADAY]
            cfg.primary_timeframe = Timeframe.INTRADAY
            cfg.trading_mode = TradingMode.PAPER
            cfg.starting_capital_usd = 1000.0
            cfg.storage.model_dir = "/tmp/models"
            mock_cfg.return_value = cfg
            orch = Orchestrator(storage, fetcher)
        return orch

    @pytest.mark.asyncio
    async def test_train_models_creates_trainer_and_detector(self):
        orch = self._orch_for_train()
        fm = MagicMock()
        fm.features = MagicMock()
        dir_result = MagicMock()
        dir_result.oos_sharpe = 1.5
        dir_result.live_gate_pass = True
        dir_result.model = MagicMock()
        dir_result.to_metrics_record = MagicMock(return_value=MagicMock())
        meta_result = MagicMock()
        meta_result.oos_sharpe = 1.2
        meta_result.live_gate_pass = True
        meta_result.to_metrics_record = MagicMock(return_value=MagicMock())
        trainer = MagicMock()
        trainer.train_direction = MagicMock(return_value=dir_result)
        trainer.train_meta_label = MagicMock(return_value=meta_result)
        trainer.save = MagicMock()
        detector = MagicMock()
        detector.fit = MagicMock()
        detector.save = MagicMock()

        with (
            patch("src.engine.orchestrator.build_feature_matrix", return_value=fm),
            patch("src.engine.orchestrator.ModelTrainer", return_value=trainer),
            patch("src.engine.orchestrator.RegimeDetector", return_value=detector),
        ):
            await orch._train_models(Timeframe.INTRADAY)

        assert Timeframe.INTRADAY.value in orch._trainers
        assert Timeframe.INTRADAY.value in orch._detectors

    @pytest.mark.asyncio
    async def test_train_models_stores_error_on_xgb_failure(self):
        orch = self._orch_for_train()
        fm = MagicMock()
        fm.features = MagicMock()
        trainer = MagicMock()
        trainer.train_direction = MagicMock(side_effect=RuntimeError("xgb fail"))
        detector = MagicMock()
        detector.fit = MagicMock()
        detector.save = MagicMock()

        with (
            patch("src.engine.orchestrator.build_feature_matrix", return_value=fm),
            patch("src.engine.orchestrator.ModelTrainer", return_value=trainer),
            patch("src.engine.orchestrator.RegimeDetector", return_value=detector),
        ):
            # xgb failure is caught internally — should not raise
            await orch._train_models(Timeframe.INTRADAY)

        # trainer is still registered even if training fails (so retrain can be attempted)
        assert Timeframe.INTRADAY.value in orch._trainers

    @pytest.mark.asyncio
    async def test_train_models_skips_on_insufficient_bars(self):
        orch = self._orch_for_train()
        orch._storage.fetch_bars = AsyncMock(return_value=_make_bars(50))  # < 300

        with (
            patch("src.engine.orchestrator.ModelTrainer") as mock_mt,
            patch("src.engine.orchestrator.RegimeDetector"),
        ):
            await orch._train_models(Timeframe.INTRADAY)

        # ModelTrainer should never be instantiated
        mock_mt.assert_not_called()

