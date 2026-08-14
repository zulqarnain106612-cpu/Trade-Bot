"""
Comprehensive coverage tests for src/engine/orchestrator.py.

Coverage target: 10% → 70%+. Every major code path exercised with mocked
external dependencies — no real network, DB, or model I/O.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
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
    s.fetch_bars = AsyncMock(return_value=_make_bars())  # ≥300 rows for trainer
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
        patch("src.engine.orchestrator.ModelTrainer.load_direction", return_value=direction_model),
        patch("src.engine.orchestrator.ModelTrainer.load_meta", return_value=meta_model),
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
                fetcher.bootstrap_history = AsyncMock(side_effect=RuntimeError("exchange down"))
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
            with patch("src.engine.orchestrator.SignalEngine", return_value=mock_engine):
                await orch.startup()
        assert Timeframe.INTRADAY.value in orch._engines

    @pytest.mark.asyncio
    async def test_startup_missing_ensemble_falls_back_to_none(self):
        """No saved ensemble file yet (fresh deployment) -- FileNotFoundError
        must not block bringing up the direction/meta-driven engine."""
        storage = _make_storage()
        fetcher = _make_fetcher()
        orch = _make_orch(storage, fetcher)
        captured_kwargs: dict = {}

        def _capture_signal_engine(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        with _orch_patches() as (_, __, ___):
            with (
                patch(
                    "src.engine.orchestrator.ModelTrainer.load_ensemble",
                    side_effect=FileNotFoundError("no ensemble yet"),
                ),
                patch("src.engine.orchestrator.SignalEngine", side_effect=_capture_signal_engine),
            ):
                await orch.startup()
        assert captured_kwargs["ensemble"] is None

    @pytest.mark.asyncio
    async def test_startup_ensemble_load_error_logs_and_falls_back_to_none(self):
        """A corrupt/tampered ensemble file (manifest mismatch etc.) must
        degrade to no-ensemble, not block startup."""
        storage = _make_storage()
        fetcher = _make_fetcher()
        orch = _make_orch(storage, fetcher)
        captured_kwargs: dict = {}

        def _capture_signal_engine(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        with _orch_patches() as (_, __, ___):
            with (
                patch(
                    "src.engine.orchestrator.ModelTrainer.load_ensemble",
                    side_effect=RuntimeError("integrity check FAILED"),
                ),
                patch("src.engine.orchestrator.SignalEngine", side_effect=_capture_signal_engine),
            ):
                await orch.startup()
        assert captured_kwargs["ensemble"] is None


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
            tradeable=False,
            direction=0,
            p_long=0.3,
            p_bet=0.3,
            kelly_result=None,
            regime=None,
            gate_result=None,
            skip_reason="test",
        )
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=skip_result)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}
        orch._tick_counts = {Timeframe.INTRADAY.value: 0}
        orch._last_tick_ts = {Timeframe.INTRADAY.value: 0.0}

        storage = _make_storage()
        orch._storage = storage

        with patch(
            "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
        ):
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

        kr = KellyResult(
            kelly_fraction=0.05,
            adjusted_fraction=0.05,
            capital_usd=10000.0,
            entry_price=42000.0,
            quantity=0.001,
            notional_usd=42.0,
            is_capped=False,
        )
        tradeable = SignalResult(
            tradeable=True,
            direction=1,
            p_long=0.75,
            p_bet=0.7,
            kelly_result=kr,
            regime=None,
            gate_result=None,
            skip_reason=None,
        )
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=tradeable)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}
        orch._tick_counts = {Timeframe.INTRADAY.value: 0}
        orch._last_tick_ts = {Timeframe.INTRADAY.value: 0.0}

        with (
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics"),
        ):
            await orch._tick(Timeframe.INTRADAY)

        executor.submit_signal.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_persists_ensemble_fields_when_present(self):
        from src.engine.signal_engine import SignalResult
        from src.risk.kelly import KellyResult

        orch = _make_orch()
        executor = _make_executor()
        executor.submit_signal = AsyncMock(return_value=("trade-123", "opened"))
        executor.get_current_equity = AsyncMock(return_value=1000.0)
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor
        storage = _make_storage()
        orch._storage = storage
        orch._storage.latest_close = AsyncMock(return_value=None)

        kr = KellyResult(
            kelly_fraction=0.05,
            adjusted_fraction=0.05,
            capital_usd=10000.0,
            entry_price=42000.0,
            quantity=0.001,
            notional_usd=42.0,
            is_capped=False,
        )
        tradeable = SignalResult(
            tradeable=True,
            direction=1,
            p_long=0.6,
            p_bet=0.7,
            kelly_result=kr,
            regime=None,
            gate_result=None,
            skip_reason=None,
            ensemble_point_estimate=0.55,
            ensemble_blend_weight=0.3,
        )
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=tradeable)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}
        orch._tick_counts = {Timeframe.INTRADAY.value: 0}
        orch._last_tick_ts = {Timeframe.INTRADAY.value: 0.0}

        with (
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics"),
        ):
            await orch._tick(Timeframe.INTRADAY)

        storage.update_trade_ensemble_fields.assert_called_once_with("trade-123", 0.55, 0.3)

    @pytest.mark.asyncio
    async def test_tick_skips_ensemble_persist_when_not_blended(self):
        from src.engine.signal_engine import SignalResult
        from src.risk.kelly import KellyResult

        orch = _make_orch()
        executor = _make_executor()
        executor.submit_signal = AsyncMock(return_value=("trade-123", "opened"))
        executor.get_current_equity = AsyncMock(return_value=1000.0)
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor
        storage = _make_storage()
        orch._storage = storage
        orch._storage.latest_close = AsyncMock(return_value=None)

        kr = KellyResult(
            kelly_fraction=0.05,
            adjusted_fraction=0.05,
            capital_usd=10000.0,
            entry_price=42000.0,
            quantity=0.001,
            notional_usd=42.0,
            is_capped=False,
        )
        tradeable = SignalResult(
            tradeable=True,
            direction=1,
            p_long=0.75,
            p_bet=0.7,
            kelly_result=kr,
            regime=None,
            gate_result=None,
            skip_reason=None,
        )
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=tradeable)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}
        orch._tick_counts = {Timeframe.INTRADAY.value: 0}
        orch._last_tick_ts = {Timeframe.INTRADAY.value: 0.0}

        with (
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics"),
        ):
            await orch._tick(Timeframe.INTRADAY)

        storage.update_trade_ensemble_fields.assert_not_called()

    @pytest.mark.asyncio
    async def test_tick_logs_missed_trade_when_not_opened(self):
        """UI-001: any outcome other than 'opened' is logged as a missed trade."""
        from src.engine.signal_engine import SignalResult
        from src.regime.detector import RegimePrediction
        from src.risk.kelly import KellyResult

        orch = _make_orch()
        executor = _make_executor()
        executor.submit_signal = AsyncMock(return_value=(None, "rejected"))
        executor.get_current_equity = AsyncMock(return_value=1000.0)
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor
        storage = _make_storage()
        storage.latest_close = AsyncMock(return_value=None)
        storage.insert_missed_trade = AsyncMock(return_value=None)
        orch._storage = storage

        kr = KellyResult(
            kelly_fraction=0.05,
            adjusted_fraction=0.05,
            capital_usd=10000.0,
            entry_price=42000.0,
            quantity=0.001,
            notional_usd=42.0,
            is_capped=False,
        )
        tradeable = SignalResult(
            tradeable=True,
            direction=1,
            p_long=0.75,
            p_bet=0.7,
            kelly_result=kr,
            regime=RegimePrediction(
                state=1, prob_ranging=0.2, prob_trending=0.7, prob_volatile=0.1
            ),
            gate_result=None,
            skip_reason=None,
        )
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=tradeable)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}
        orch._tick_counts = {Timeframe.INTRADAY.value: 0}
        orch._last_tick_ts = {Timeframe.INTRADAY.value: 0.0}

        with (
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics"),
        ):
            await orch._tick(Timeframe.INTRADAY)

        storage.insert_missed_trade.assert_called_once()
        logged = storage.insert_missed_trade.call_args.args[0]
        assert logged.reason == "rejected"
        assert logged.regime_at_entry == 1

    @pytest.mark.asyncio
    async def test_tick_missed_trade_log_failure_does_not_raise(self):
        """A storage failure while logging a missed trade must never break the trade path."""
        from src.engine.signal_engine import SignalResult
        from src.risk.kelly import KellyResult

        orch = _make_orch()
        executor = _make_executor()
        executor.submit_signal = AsyncMock(return_value=(None, "skipped"))
        executor.get_current_equity = AsyncMock(return_value=1000.0)
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor
        storage = _make_storage()
        storage.latest_close = AsyncMock(return_value=None)
        storage.insert_missed_trade = AsyncMock(side_effect=RuntimeError("db down"))
        orch._storage = storage

        kr = KellyResult(
            kelly_fraction=0.05,
            adjusted_fraction=0.05,
            capital_usd=10000.0,
            entry_price=42000.0,
            quantity=0.001,
            notional_usd=42.0,
            is_capped=False,
        )
        tradeable = SignalResult(
            tradeable=True,
            direction=0,
            p_long=0.3,
            p_bet=0.4,
            kelly_result=kr,
            regime=None,
            gate_result=None,
            skip_reason=None,
        )
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=tradeable)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}
        orch._tick_counts = {Timeframe.INTRADAY.value: 0}
        orch._last_tick_ts = {Timeframe.INTRADAY.value: 0.0}

        with (
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics"),
        ):
            await orch._tick(Timeframe.INTRADAY)  # must not raise despite storage failure

        storage.insert_missed_trade.assert_called_once()


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


# ---------------------------------------------------------------------------
# Tests: _sleep_until_next_bar
# ---------------------------------------------------------------------------


class TestSleepUntilNextBar:
    def _make_orch(self):
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
            cfg.storage.model_dir = "/tmp/models"
            mock_cfg.return_value = cfg
            orch = Orchestrator(storage, fetcher)
        return orch

    @pytest.mark.asyncio
    async def test_sleep_until_next_bar_calls_sleep(self):
        orch = self._make_orch()
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with patch("src.engine.orchestrator.time") as mock_time:
                mock_time.monotonic.return_value = 1_700_000_000.0
                # 900s bar, 100s elapsed since last bar → sleep ~800s
                await orch._sleep_until_next_bar(900)
        mock_sleep.assert_called_once()
        slept = mock_sleep.call_args[0][0]
        assert slept >= 0


# ---------------------------------------------------------------------------
# Tests: _midnight_reset_loop
# ---------------------------------------------------------------------------


class TestMidnightResetLoop:
    def _make_orch(self):
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
            cfg.storage.model_dir = "/tmp/models"
            mock_cfg.return_value = cfg
            orch = Orchestrator(storage, fetcher)
        return orch

    @pytest.mark.asyncio
    async def test_midnight_reset_calls_executor_reset(self):
        """Loop fires reset on executor after sleep."""
        orch = self._make_orch()
        orch._running = True
        executor = _make_executor()
        executor.reset_daily_equity = AsyncMock(return_value=1000.0)
        orch._executor = executor

        call_count = 0

        async def _fake_sleep(_s):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                orch._running = False  # stop after first iteration

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await orch._midnight_reset_loop()

        executor.reset_daily_equity.assert_called_once()

    @pytest.mark.asyncio
    async def test_midnight_reset_no_executor_does_not_raise(self):
        orch = self._make_orch()
        orch._executor = None

        call_count = 0

        async def _fake_sleep(_s):
            nonlocal call_count
            call_count += 1
            orch._running = False

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await orch._midnight_reset_loop()  # should not raise

    @pytest.mark.asyncio
    async def test_midnight_reset_cancelled_exits_cleanly(self):
        orch = self._make_orch()

        async def _cancel(_s):
            raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=_cancel):
            await orch._midnight_reset_loop()  # CancelledError swallowed

    @pytest.mark.asyncio
    async def test_midnight_reset_exception_retries(self):
        """Non-CancelledError exception → sleep 60s then retry."""
        orch = self._make_orch()
        orch._running = True
        executor = _make_executor()
        executor.reset_daily_equity = AsyncMock(side_effect=RuntimeError("db down"))
        orch._executor = executor

        iteration = 0

        async def _fake_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration == 2:  # second sleep is the 60s retry sleep
                orch._running = False

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await orch._midnight_reset_loop()

        assert iteration == 2  # first sleep (wait for midnight) + retry sleep


# ---------------------------------------------------------------------------
# Tests: _position_monitor_loop
# ---------------------------------------------------------------------------


class TestPositionMonitorLoop:
    def _make_orch(self):
        from src.engine.orchestrator import Orchestrator

        storage = _make_storage()
        storage.latest_close = AsyncMock(return_value=None)
        fetcher = _make_fetcher()
        fetcher.fetch_ticker_price = AsyncMock(return_value=50_000.0)
        with patch("src.engine.orchestrator.get_settings") as mock_cfg:
            cfg = MagicMock()
            cfg.primary_symbol = "BTC/USDT"
            cfg.active_timeframes = [Timeframe.INTRADAY]
            cfg.primary_timeframe = Timeframe.INTRADAY
            cfg.trading_mode = TradingMode.PAPER
            cfg.starting_capital_usd = 1000.0
            cfg.storage.model_dir = "/tmp/models"
            cfg.risk.position_monitor_interval_s = 5
            mock_cfg.return_value = cfg
            orch = Orchestrator(storage, fetcher)
        return orch

    @pytest.mark.asyncio
    async def test_position_monitor_no_executor_skips(self):
        orch = self._make_orch()
        orch._executor = None
        iteration = 0

        async def _fake_sleep(_s):
            nonlocal iteration
            iteration += 1
            orch._running = False

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await orch._position_monitor_loop()

        # No executor → no price fetch needed
        assert orch._fetcher.fetch_ticker_price.await_count == 0

    @pytest.mark.asyncio
    async def test_position_monitor_no_positions_skips_price_fetch(self):
        orch = self._make_orch()
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor
        iteration = 0

        async def _fake_sleep(_s):
            nonlocal iteration
            iteration += 1
            orch._running = False

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await orch._position_monitor_loop()

        orch._fetcher.fetch_ticker_price.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_position_monitor_closes_on_stop_loss(self):
        orch = self._make_orch()
        orch._running = True
        executor = _make_executor()
        pos = {
            "symbol": "BTC/USDT",
            "trade_id": "t1",
            "unrealized_pnl_pct": -0.06,  # triggers stop-loss
            "entry_ts": 1_000_000,
        }
        executor.open_positions_safe = AsyncMock(
            side_effect=[
                [pos],  # first snapshot (has position)
                [pos],  # second snapshot (after mark_to_market)
            ]
        )
        executor.mark_to_market = AsyncMock(return_value=None)
        executor.close_position = AsyncMock(return_value=-60.0)
        orch._executor = executor

        iteration = 0

        async def _fake_sleep(_s):
            nonlocal iteration
            iteration += 1
            orch._running = False

        controls = {
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.05,
            "take_profit_enabled": False,
            "take_profit_pct": 0.1,
            "max_holding_period_s": 86400.0,
        }
        with (
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch(
                "src.engine.orchestrator.runtime_config.get_risk_controls",
                new_callable=AsyncMock,
                return_value=controls,
            ),
            patch(
                "src.engine.orchestrator.check_position_exit",
                return_value="stop_loss",
            ),
        ):
            await orch._position_monitor_loop()

        executor.close_position.assert_awaited_once_with(
            trade_id="t1", exit_price=50_000.0, exit_reason="stop_loss"
        )

    @pytest.mark.asyncio
    async def test_position_monitor_price_zero_skips(self):
        """Price fetch returns 0 → skip close check for safety."""
        orch = self._make_orch()
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(
            return_value=[{"symbol": "BTC/USDT", "trade_id": "t1"}]
        )
        executor.mark_to_market = AsyncMock()
        orch._executor = executor
        orch._fetcher.fetch_ticker_price = AsyncMock(return_value=0.0)

        iteration = 0

        async def _fake_sleep(_s):
            nonlocal iteration
            iteration += 1
            orch._running = False

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await orch._position_monitor_loop()

        executor.mark_to_market.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_position_monitor_cancelled_exits_cleanly(self):
        orch = self._make_orch()

        async def _cancel(_s):
            raise asyncio.CancelledError

        with patch("asyncio.sleep", side_effect=_cancel):
            await orch._position_monitor_loop()


# ---------------------------------------------------------------------------
# Tests: _tick — correlation_scalar failure falls back to 1.0
# ---------------------------------------------------------------------------


class TestTickCorrelationFallback:
    def _make_orch(self):
        from src.engine.orchestrator import Orchestrator

        storage = _make_storage()
        storage.latest_close = AsyncMock(return_value=None)
        storage.latest_bar_ts = AsyncMock(return_value=None)
        storage.upsert_regime_snapshot = AsyncMock(return_value=None)
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

    def _make_orch_live(self):
        from src.engine.orchestrator import Orchestrator

        storage = _make_storage()
        storage.latest_close = AsyncMock(return_value=None)
        storage.latest_bar_ts = AsyncMock(return_value=None)
        storage.upsert_regime_snapshot = AsyncMock(return_value=None)
        storage.earliest_equity_ts = AsyncMock(return_value=None)
        fetcher = _make_fetcher()
        with patch("src.engine.orchestrator.get_settings") as mock_cfg:
            cfg = MagicMock()
            cfg.primary_symbol = "BTC/USDT"
            cfg.active_timeframes = [Timeframe.INTRADAY]
            cfg.primary_timeframe = Timeframe.INTRADAY
            cfg.trading_mode = TradingMode.LIVE
            cfg.starting_capital_usd = 1000.0
            cfg.storage.model_dir = "/tmp/models"
            mock_cfg.return_value = cfg
            orch = Orchestrator(storage, fetcher)
        return orch

    @pytest.mark.asyncio
    async def test_tick_live_mode_no_paper_equity_history_yet(self):
        """TRADING_MODE=live with no recorded paper-equity history
        (earliest_equity_ts returns None) must not crash paper_trading_days
        computation -- it simply stays 0."""
        orch = self._make_orch_live()
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor

        signal = MagicMock()
        signal.tradeable = False
        signal.regime = None
        signal.p_long = 0.5
        signal.p_bet = 0.5
        signal.kelly_result = None
        engine = AsyncMock()
        engine.tick = AsyncMock(return_value=signal)
        orch._engines[Timeframe.INTRADAY.value] = engine

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)
        tracker.push_bar_returns = MagicMock()

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics"),
        ):
            await orch._tick(Timeframe.INTRADAY)

        call_kwargs = engine.tick.call_args.kwargs
        assert call_kwargs["paper_trading_days"] == 0

    @pytest.mark.asyncio
    async def test_tick_correlation_first_tick_no_prior_close(self):
        """First-ever tick for this timeframe: _last_close_for_corr has no
        entry yet, so the bar-return push must be skipped (prev is None)
        while the close still gets cached for the next tick."""
        orch = self._make_orch()
        orch._storage.latest_close = AsyncMock(return_value=(1_700_000_000_000, 30_000.0))
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor

        signal = MagicMock()
        signal.tradeable = False
        signal.regime = None
        signal.p_long = 0.5
        signal.p_bet = 0.5
        signal.kelly_result = None
        engine = AsyncMock()
        engine.tick = AsyncMock(return_value=signal)
        orch._engines[Timeframe.INTRADAY.value] = engine

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)
        tracker.push_bar_returns = MagicMock()
        assert Timeframe.INTRADAY.value not in orch._last_close_for_corr

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics"),
        ):
            await orch._tick(Timeframe.INTRADAY)

        tracker.push_bar_returns.assert_not_called()  # no prior close to diff against
        assert orch._last_close_for_corr[Timeframe.INTRADAY.value] == (
            1_700_000_000_000,
            30_000.0,
        )

    @pytest.mark.asyncio
    async def test_tick_correlation_error_falls_back_to_1(self):
        """get_portfolio_correlation() crash must not block the tick."""
        orch = self._make_orch()
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor

        signal = MagicMock()
        signal.tradeable = False
        signal.regime = None
        signal.p_long = 0.5
        signal.p_bet = 0.5
        signal.kelly_result = None
        engine = AsyncMock()
        engine.tick = AsyncMock(return_value=signal)
        orch._engines[Timeframe.INTRADAY.value] = engine

        with (
            patch(
                "src.engine.orchestrator.get_portfolio_correlation",
                side_effect=RuntimeError("tracker broken"),
            ),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics"),
        ):
            await orch._tick(Timeframe.INTRADAY)

        # tick completed → engine.tick was called with correlation_scalar=1.0
        call_kwargs = engine.tick.call_args.kwargs
        assert call_kwargs["correlation_scalar"] == 1.0

    @pytest.mark.asyncio
    async def test_tick_drift_blocks_tradeable_signal(self):
        """Performance drift gate skips execution even when signal is tradeable."""
        orch = self._make_orch()
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor

        signal = MagicMock()
        signal.tradeable = True
        signal.regime = None
        signal.p_long = 0.8
        signal.p_bet = 0.8
        signal.kelly_result = MagicMock()
        engine = AsyncMock()
        engine.tick = AsyncMock(return_value=signal)
        orch._engines[Timeframe.INTRADAY.value] = engine

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)
        tracker.push_bar_returns = MagicMock()

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics"),
            patch.object(
                orch._drift_adapter,
                "check_drift",
                return_value={"drifted": True, "metric": "sharpe", "reason": "below floor"},
            ),
        ):
            await orch._tick(Timeframe.INTRADAY)

        # drift blocked → submit_signal must NOT be called
        executor.submit_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_drift_triggers_retrain_task(self):
        """Drift detection must schedule an immediate retrain task (not just block)."""
        orch = self._make_orch()
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor

        signal = MagicMock()
        signal.tradeable = True
        signal.regime = None
        signal.p_long = 0.8
        signal.p_bet = 0.8
        signal.kelly_result = MagicMock()
        engine = AsyncMock()
        engine.tick = AsyncMock(return_value=signal)
        orch._engines[Timeframe.INTRADAY.value] = engine

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)
        tracker.push_bar_returns = MagicMock()

        train_called = []

        async def _fake_train(tf):
            train_called.append(tf)

        orch._train_models = _fake_train

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics"),
            patch.object(
                orch._drift_adapter,
                "check_drift",
                return_value={"drifted": True, "metric": "sharpe", "reason": "below floor"},
            ),
        ):
            await orch._tick(Timeframe.INTRADAY)
            # Allow the created task to run
            await asyncio.sleep(0)

        # Signal must be blocked
        executor.submit_signal.assert_not_called()
        # Retrain must have been triggered
        assert len(train_called) == 1, "drift should trigger one retrain task"
        assert train_called[0] == Timeframe.INTRADAY

    @pytest.mark.asyncio
    async def test_drift_retrain_skipped_when_prior_task_still_running(self):
        """Overlap guard: a still-running retrain task must not be duplicated
        by a second drift trigger on the same timeframe."""
        orch = self._make_orch()
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor

        signal = MagicMock()
        signal.tradeable = True
        signal.regime = None
        signal.p_long = 0.8
        signal.p_bet = 0.8
        signal.kelly_result = MagicMock()
        engine = AsyncMock()
        engine.tick = AsyncMock(return_value=signal)
        orch._engines[Timeframe.INTRADAY.value] = engine

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)
        tracker.push_bar_returns = MagicMock()

        # A prior retrain task for this timeframe that never completes during
        # the test -- prior.done() must read False.
        never_done: asyncio.Future = asyncio.get_running_loop().create_future()
        orch._retrain_tasks[Timeframe.INTRADAY.value] = never_done  # type: ignore[assignment]

        train_called = []

        async def _fake_train(tf):
            train_called.append(tf)

        orch._train_models = _fake_train

        try:
            with (
                patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
                patch(
                    "src.engine.orchestrator.compute_win_loss_stats",
                    return_value=(0, 0.0, 0.0, 0.0),
                ),
                patch("src.engine.orchestrator.update_metrics"),
                patch.object(
                    orch._drift_adapter,
                    "check_drift",
                    return_value={"drifted": True, "metric": "sharpe", "reason": "below floor"},
                ),
            ):
                await orch._tick(Timeframe.INTRADAY)
                await asyncio.sleep(0)
        finally:
            never_done.cancel()

        assert train_called == []  # no new retrain scheduled -- prior still running
        assert orch._retrain_tasks[Timeframe.INTRADAY.value] is never_done

    @pytest.mark.asyncio
    async def test_drift_retrain_done_callback_skips_del_when_task_already_replaced(self):
        """If _retrain_tasks[tf] has already been reassigned to a different
        task by the time this (now-finished) task's done-callback fires,
        the callback must not delete the newer task's registration."""
        orch = self._make_orch()
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor

        signal = MagicMock()
        signal.tradeable = True
        signal.regime = None
        signal.p_long = 0.8
        signal.p_bet = 0.8
        signal.kelly_result = MagicMock()
        engine = AsyncMock()
        engine.tick = AsyncMock(return_value=signal)
        orch._engines[Timeframe.INTRADAY.value] = engine

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)
        tracker.push_bar_returns = MagicMock()

        async def _fast_train(tf):
            return None

        orch._train_models = _fast_train

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics"),
            patch.object(
                orch._drift_adapter,
                "check_drift",
                return_value={"drifted": True, "metric": "sharpe", "reason": "below floor"},
            ),
        ):
            await orch._tick(Timeframe.INTRADAY)
            # The task was created but has not run yet (create_task only
            # schedules it) -- swap in a decoy before it gets a chance to run.
            original_task = orch._retrain_tasks[Timeframe.INTRADAY.value]
            decoy = asyncio.get_running_loop().create_future()
            orch._retrain_tasks[Timeframe.INTRADAY.value] = decoy  # type: ignore[assignment]
            await original_task  # let it run to completion and fire its callback

        # The decoy must still be registered -- the stale callback must not
        # have deleted it.
        assert orch._retrain_tasks[Timeframe.INTRADAY.value] is decoy
        decoy.cancel()

    @pytest.mark.asyncio
    async def test_drift_retrain_failure_records_last_error(self):
        """A failed drift-triggered retrain's done-callback must record the
        error and clear the (now-finished) task slot."""
        orch = self._make_orch()
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor

        signal = MagicMock()
        signal.tradeable = True
        signal.regime = None
        signal.p_long = 0.8
        signal.p_bet = 0.8
        signal.kelly_result = MagicMock()
        engine = AsyncMock()
        engine.tick = AsyncMock(return_value=signal)
        orch._engines[Timeframe.INTRADAY.value] = engine

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)
        tracker.push_bar_returns = MagicMock()

        async def _failing_train(tf):
            raise RuntimeError("retrain blew up")

        orch._train_models = _failing_train

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics"),
            patch.object(
                orch._drift_adapter,
                "check_drift",
                return_value={"drifted": True, "metric": "sharpe", "reason": "below floor"},
            ),
        ):
            await orch._tick(Timeframe.INTRADAY)
            # Let the scheduled task run to completion and its done-callback fire.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert "retrain blew up" in orch._last_retrain_error[Timeframe.INTRADAY.value]
        assert Timeframe.INTRADAY.value not in orch._retrain_tasks
