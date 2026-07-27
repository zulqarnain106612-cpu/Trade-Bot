"""Additional orchestrator coverage targeting uncovered paths."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Timeframe, TradingMode
from src.data.storage import BarRecord
from src.engine.signal_engine import SignalResult
from src.risk.kelly import KellyResult


def _make_bars(n: int = 350) -> list[BarRecord]:
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


def _make_storage():
    s = AsyncMock()
    s.health_check = AsyncMock(return_value=True)
    s.latest_model_metrics = AsyncMock(return_value=None)
    s.fetch_trades = AsyncMock(return_value=[])
    s.earliest_equity_ts = AsyncMock(return_value=None)
    s.insert_bar = AsyncMock(return_value=None)
    s.fetch_bars = AsyncMock(return_value=_make_bars())
    s.initialize = AsyncMock(return_value=None)
    s.close = AsyncMock(return_value=None)
    s.latest_close = AsyncMock(return_value=None)
    s.latest_bar_ts = AsyncMock(return_value=1_700_000_000_000)
    s.upsert_regime_snapshot = AsyncMock(return_value=None)
    return s


def _make_fetcher():
    f = AsyncMock()
    f.bootstrap_history = AsyncMock(return_value=100)
    f.fetch_ticker_price = AsyncMock(return_value=50_000.0)
    return f


def _make_executor():
    e = AsyncMock()
    e.initialize = AsyncMock(return_value=None)
    e.get_daily_pnl = AsyncMock(return_value=0.0)
    e.get_consecutive_losses = AsyncMock(return_value=0)
    e.equity_usd = 100_000.0
    e.starting_equity_usd = 100_000.0
    e.shutdown = AsyncMock(return_value=None)
    e.open_positions_safe = AsyncMock(return_value=[])
    e.submit_signal = AsyncMock(return_value=("trade-123", "opened"))
    e.get_current_equity = AsyncMock(return_value=100_000.0)
    return e


def _make_orch():
    from src.engine.orchestrator import Orchestrator

    storage = _make_storage()
    fetcher = _make_fetcher()
    with patch("src.engine.orchestrator.get_settings") as mock_cfg:
        cfg = MagicMock()
        cfg.primary_symbol = "BTC/USDT"
        cfg.active_timeframes = [Timeframe.INTRADAY]
        cfg.primary_timeframe = Timeframe.INTRADAY
        cfg.trading_mode = TradingMode.PAPER
        cfg.starting_capital_usd = 100_000.0
        cfg.storage.model_dir = "/tmp/models"
        mock_cfg.return_value = cfg
        orch = Orchestrator(storage, fetcher)
    return orch


def _make_skip_result() -> SignalResult:
    return SignalResult(
        tradeable=False,
        direction=0,
        p_long=0.3,
        p_bet=0.3,
        kelly_result=None,
        regime=None,
        gate_result=None,
        skip_reason="test",
    )


def _make_tradeable_result(entry_price: float = 42_000.0) -> SignalResult:
    kr = KellyResult(
        kelly_fraction=0.05,
        adjusted_fraction=0.05,
        capital_usd=100_000.0,
        entry_price=entry_price,
        quantity=0.01,
        notional_usd=entry_price * 0.01,
        is_capped=False,
    )
    return SignalResult(
        tradeable=True,
        direction=1,
        p_long=0.75,
        p_bet=0.7,
        kelly_result=kr,
        regime=None,
        gate_result=None,
        skip_reason=None,
    )


# ---------------------------------------------------------------------------
# run() method — start then stop
# ---------------------------------------------------------------------------


async def _noop_loop(self_or_none=None) -> None:
    """Replacement for background loops that would sleep for hours."""
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.sleep(3600)


class TestOrchestratorRun:
    @pytest.mark.asyncio
    async def test_run_starts_and_stops(self):
        orch = _make_orch()
        executor = _make_executor()
        orch._executor = executor
        orch._engines = {}

        async def _stop_after():
            await asyncio.sleep(0.02)
            orch.stop()

        stopper = asyncio.create_task(_stop_after())
        with (
            patch.object(orch, "_midnight_reset_loop", side_effect=_noop_loop),
            patch.object(orch, "_position_monitor_loop", side_effect=_noop_loop),
        ):
            await orch.run()
        await stopper

        assert orch._running is False

    @pytest.mark.asyncio
    async def test_run_sets_running_true_then_false(self):
        orch = _make_orch()
        executor = _make_executor()
        orch._executor = executor
        orch._engines = {}
        seen_running = []

        async def _observe_and_stop():
            seen_running.append(orch._running)
            await asyncio.sleep(0.01)
            orch.stop()

        observer = asyncio.create_task(_observe_and_stop())
        with (
            patch.object(orch, "_midnight_reset_loop", side_effect=_noop_loop),
            patch.object(orch, "_position_monitor_loop", side_effect=_noop_loop),
        ):
            await orch.run()
        await observer

        assert orch._running is False


# ---------------------------------------------------------------------------
# _timeframe_loop() — cancellation and error paths
# ---------------------------------------------------------------------------


class TestTimeframeLoop:
    @pytest.mark.asyncio
    async def test_timeframe_loop_cancelled_exits_cleanly(self):
        orch = _make_orch()
        orch._running = True

        with patch.object(
            orch, "_sleep_until_next_bar", new=AsyncMock(side_effect=asyncio.CancelledError)
        ):
            # CancelledError is caught inside the loop, which then breaks — no exception propagates
            await orch._timeframe_loop(Timeframe.INTRADAY)
        # Reaching here means the loop exited cleanly

    @pytest.mark.asyncio
    async def test_timeframe_loop_error_then_stops(self):
        orch = _make_orch()
        orch._running = True

        call_count = [0]

        async def _sleep(*args):
            call_count[0] += 1
            if call_count[0] > 1:
                orch._running = False

        with (
            patch.object(
                orch, "_sleep_until_next_bar", new=AsyncMock(side_effect=RuntimeError("boom"))
            ),
            patch("asyncio.sleep", new=AsyncMock(side_effect=_sleep)),
        ):
            await orch._timeframe_loop(Timeframe.INTRADAY)

        assert call_count[0] >= 1


# ---------------------------------------------------------------------------
# _tick() — uncovered branches
# ---------------------------------------------------------------------------


class TestTickUncoveredBranches:
    def _make_orch_with_state(self):
        orch = _make_orch()
        executor = _make_executor()
        orch._executor = executor
        orch._storage = _make_storage()
        orch._tick_counts = {Timeframe.INTRADAY.value: 0}
        orch._last_tick_ts = {Timeframe.INTRADAY.value: 0.0}
        orch._last_close_for_corr = {}
        return orch, executor

    @pytest.mark.asyncio
    async def test_tick_with_dir_meta_metrics_not_none(self):
        orch, _executor = self._make_orch_with_state()

        # dir_metrics and meta_metrics are both not None
        dir_metrics = MagicMock()
        dir_metrics.live_gate_pass = True
        meta_metrics = MagicMock()
        meta_metrics.live_gate_pass = True
        orch._storage.latest_model_metrics = AsyncMock(side_effect=[dir_metrics, meta_metrics])

        result = _make_skip_result()
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=result)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)
        tracker.push_bar_returns = MagicMock()

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
        ):
            await orch._tick(Timeframe.INTRADAY)

    @pytest.mark.asyncio
    async def test_tick_with_latest_close_updates_correlation(self):
        orch, _executor = self._make_orch_with_state()
        # Set previous close so bar_return is computed
        orch._last_close_for_corr[Timeframe.INTRADAY.value] = (999_000, 40_000.0)
        orch._storage.latest_close = AsyncMock(return_value=(1_000_000, 41_000.0))

        result = _make_skip_result()
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=result)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=0.9)
        tracker.push_bar_returns = MagicMock()

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
        ):
            await orch._tick(Timeframe.INTRADAY)

        tracker.push_bar_returns.assert_called_once()

    @pytest.mark.asyncio
    async def test_tick_with_regime_not_none_persists_snapshot(self):
        orch, _executor = self._make_orch_with_state()

        regime = MagicMock()
        regime.state = 1
        regime.prob_ranging = 0.3
        regime.prob_trending = 0.5
        regime.prob_volatile = 0.2

        result = SignalResult(
            tradeable=False,
            direction=0,
            p_long=0.4,
            p_bet=0.4,
            kelly_result=None,
            regime=regime,
            gate_result=None,
            skip_reason="skip",
        )
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=result)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}

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

        orch._storage.upsert_regime_snapshot.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tick_ticker_fetch_failure_skips_signal(self):
        orch, executor = self._make_orch_with_state()

        kr = KellyResult(
            kelly_fraction=0.05,
            adjusted_fraction=0.05,
            capital_usd=100_000.0,
            entry_price=0.0,  # 0 price triggers fetch
            quantity=0.01,
            notional_usd=420.0,
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
        orch._fetcher.fetch_ticker_price = AsyncMock(side_effect=RuntimeError("network down"))

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics"),
        ):
            await orch._tick(Timeframe.INTRADAY)

        # Signal should be skipped due to fetch failure
        executor.submit_signal.assert_not_called()

    @pytest.mark.asyncio
    async def test_tick_live_mode_checks_paper_equity(self):
        orch, _executor = self._make_orch_with_state()
        orch._cfg.trading_mode = TradingMode.LIVE
        orch._storage.earliest_equity_ts = AsyncMock(return_value=1_700_000_000_000)

        result = _make_skip_result()
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=result)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
        ):
            await orch._tick(Timeframe.INTRADAY)

        orch._storage.earliest_equity_ts.assert_awaited_once()


# ---------------------------------------------------------------------------
# startup() — LIVE mode, drift detector init, engine-build skip branches
# ---------------------------------------------------------------------------


def _make_orch_cfg(trading_mode=TradingMode.PAPER):
    from src.engine.orchestrator import Orchestrator

    storage = _make_storage()
    fetcher = _make_fetcher()
    with patch("src.engine.orchestrator.get_settings") as mock_cfg:
        cfg = MagicMock()
        cfg.primary_symbol = "BTC/USDT"
        cfg.active_timeframes = [Timeframe.INTRADAY]
        cfg.primary_timeframe = Timeframe.INTRADAY
        cfg.trading_mode = trading_mode
        cfg.starting_capital_usd = 100_000.0
        cfg.storage.model_dir = "/tmp/models"
        mock_cfg.return_value = cfg
        orch = Orchestrator(storage, fetcher)
    return orch, storage, fetcher


def _startup_common_patches(executor):
    """Common patch set for startup() happy-path pieces unrelated to the branch under test."""
    mon = MagicMock()
    mon.register_probe = MagicMock()
    mon.register_tick_source = MagicMock()
    mon.start = AsyncMock(return_value=None)
    return (
        patch("src.engine.orchestrator.get_monitor", return_value=mon),
        patch(
            "src.engine.orchestrator.run_pipeline_selftest",
            return_value={"passed": True, "n_rows": 50},
        ),
    )


class TestStartupLiveModeAndDriftDetector:
    @pytest.mark.asyncio
    async def test_startup_live_mode_creates_live_executor(self):
        orch, _storage, _fetcher = _make_orch_cfg(trading_mode=TradingMode.LIVE)
        executor = _make_executor()
        p1, p2 = _startup_common_patches(executor)
        with (
            p1,
            p2,
            patch("src.engine.orchestrator.LiveExecutor", return_value=executor) as live_cls,
            patch("src.engine.orchestrator.PaperExecutor") as paper_cls,
        ):
            await orch.startup()
        live_cls.assert_called_once()
        paper_cls.assert_not_called()
        assert orch._executor is executor

    @pytest.mark.asyncio
    async def test_startup_drift_detector_initialized_on_success(self):
        orch, _storage, _fetcher = _make_orch_cfg()
        executor = _make_executor()
        p1, p2 = _startup_common_patches(executor)

        trainer = MagicMock()
        trainer.train_sharpe = 2.0
        trainer.oos_sharpe = 1.5
        trainer.train_accuracy = 0.6
        trainer.oos_accuracy = 0.58
        trainer.train_win_rate = 0.55
        trainer.max_drawdown_pct = 0.1
        trainer.trades_count = 400

        async def _fake_train(tf):
            orch._trainers[tf.value] = trainer
            orch._detectors[tf.value] = MagicMock()

        orch._train_models = _fake_train

        with (
            p1,
            p2,
            patch("src.engine.orchestrator.PaperExecutor", return_value=executor),
        ):
            await orch.startup()

        assert orch._drift_detector is not None

    @pytest.mark.asyncio
    async def test_startup_drift_detector_init_failure_continues(self):
        orch, _storage, _fetcher = _make_orch_cfg()
        executor = _make_executor()
        p1, p2 = _startup_common_patches(executor)

        trainer = MagicMock()

        async def _fake_train(tf):
            orch._trainers[tf.value] = trainer
            orch._detectors[tf.value] = MagicMock()

        orch._train_models = _fake_train

        with (
            p1,
            p2,
            patch("src.engine.orchestrator.PaperExecutor", return_value=executor),
            patch(
                "src.engine.orchestrator.PerformanceBaseline",
                side_effect=RuntimeError("bad baseline"),
            ),
        ):
            await orch.startup()  # must not raise

        assert orch._drift_detector is None

    @pytest.mark.asyncio
    async def test_startup_engine_skip_when_detector_or_trainer_missing(self):
        orch, _storage, _fetcher = _make_orch_cfg()
        executor = _make_executor()
        p1, p2 = _startup_common_patches(executor)

        async def _fake_train(tf):
            pass  # never populates _trainers/_detectors for this timeframe

        orch._train_models = _fake_train

        with (
            p1,
            p2,
            patch("src.engine.orchestrator.PaperExecutor", return_value=executor),
        ):
            await orch.startup()

        assert Timeframe.INTRADAY.value not in orch._engines

    @pytest.mark.asyncio
    async def test_startup_engine_skip_on_filenotfound(self):
        orch, _storage, _fetcher = _make_orch_cfg()
        executor = _make_executor()
        p1, p2 = _startup_common_patches(executor)

        async def _fake_train(tf):
            orch._trainers[tf.value] = MagicMock()
            orch._detectors[tf.value] = MagicMock()

        orch._train_models = _fake_train

        with (
            p1,
            p2,
            patch("src.engine.orchestrator.PaperExecutor", return_value=executor),
            patch(
                "src.engine.orchestrator.ModelTrainer.load_direction",
                side_effect=FileNotFoundError("no model on disk"),
            ),
        ):
            await orch.startup()

        assert Timeframe.INTRADAY.value not in orch._engines


# ---------------------------------------------------------------------------
# _timeframe_loop() — running flips False during sleep → clean break
# ---------------------------------------------------------------------------


class TestTimeframeLoopRunningFlipsDuringSleep:
    @pytest.mark.asyncio
    async def test_loop_breaks_when_running_false_after_sleep(self):
        orch = _make_orch()
        orch._running = True

        async def _sleep(_tf_seconds):
            orch._running = False  # flips False during the sleep call itself

        tick_mock = AsyncMock()
        with (
            patch.object(orch, "_sleep_until_next_bar", new=AsyncMock(side_effect=_sleep)),
            patch.object(orch, "_tick", new=tick_mock),
        ):
            await orch._timeframe_loop(Timeframe.INTRADAY)

        tick_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _tick() — update_metrics failure must not block the trade path
# ---------------------------------------------------------------------------


class TestTickUpdateMetricsFailure:
    @pytest.mark.asyncio
    async def test_update_metrics_exception_is_swallowed(self):
        orch = _make_orch()
        executor = _make_executor()
        orch._executor = executor
        orch._storage = _make_storage()
        orch._tick_counts = {Timeframe.INTRADAY.value: 0}
        orch._last_tick_ts = {Timeframe.INTRADAY.value: 0.0}
        orch._last_close_for_corr = {}

        result = _make_skip_result()
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=result)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)
        tracker.push_bar_returns = MagicMock()

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
            patch("src.engine.orchestrator.update_metrics", side_effect=RuntimeError("prom down")),
        ):
            await orch._tick(Timeframe.INTRADAY)  # must not raise


# ---------------------------------------------------------------------------
# _position_monitor_loop() — drift-record-on-close block
#
# Previously this lived in _tick() gated on `hasattr(outcome, "__dict__")`,
# which was always False since AbstractExecutor.submit_signal returns a
# plain `str` outcome — record_closed_trade() was silently never invoked.
# Fixed by moving the call to where a trade actually closes (close_position(),
# inside _position_monitor_loop), matching DriftIntegrationAdapter.
# record_closed_trade's own docstring ("Called by orchestrator after
# executor.close_position() completes").
# ---------------------------------------------------------------------------


class TestPositionMonitorDriftRecordOnClose:
    def _make_orch_with_position(self, pos_overrides=None):
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
            cfg.risk.position_monitor_interval_s = 5
            mock_cfg.return_value = cfg
            orch = Orchestrator(storage, fetcher)
        orch._running = True
        orch._drift_detector = MagicMock()  # truthy → enters the block
        orch._drift_adapter.record_closed_trade = AsyncMock(return_value=None)

        executor = _make_executor()
        pos = {
            "symbol": "BTC/USDT",
            "trade_id": "t1",
            "direction": "long",
            "unrealized_pnl_pct": -0.06,  # triggers stop-loss
            "entry_ts": 1_000_000,
        }
        pos.update(pos_overrides or {})
        executor.open_positions_safe = AsyncMock(side_effect=[[pos], [pos]])
        executor.mark_to_market = AsyncMock(return_value=None)
        executor.close_position = AsyncMock(return_value=-60.0)
        orch._executor = executor
        return orch, executor

    _CONTROLS = {
        "stop_loss_enabled": True,
        "stop_loss_pct": 0.05,
        "take_profit_enabled": False,
        "take_profit_pct": 0.1,
        "max_holding_period_s": 86400.0,
    }

    @pytest.mark.asyncio
    async def test_drift_record_called_on_position_close(self):
        orch, executor = self._make_orch_with_position()

        async def _fake_sleep(_s):
            orch._running = False

        with (
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch(
                "src.engine.orchestrator.runtime_config.get_risk_controls",
                new_callable=AsyncMock,
                return_value=self._CONTROLS,
            ),
        ):
            await orch._position_monitor_loop()

        orch._drift_adapter.record_closed_trade.assert_awaited_once()
        call_kwargs = orch._drift_adapter.record_closed_trade.call_args.kwargs
        assert call_kwargs["trade_id"] == "t1"
        assert call_kwargs["pnl_usd"] == -60.0
        assert call_kwargs["actual_direction"] == 1
        assert call_kwargs["current_equity"] == executor.equity_usd

    @pytest.mark.asyncio
    async def test_drift_record_short_direction(self):
        orch, _executor = self._make_orch_with_position(pos_overrides={"direction": "short"})

        async def _fake_sleep(_s):
            orch._running = False

        with (
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch(
                "src.engine.orchestrator.runtime_config.get_risk_controls",
                new_callable=AsyncMock,
                return_value=self._CONTROLS,
            ),
        ):
            await orch._position_monitor_loop()

        call_kwargs = orch._drift_adapter.record_closed_trade.call_args.kwargs
        assert call_kwargs["actual_direction"] == -1

    @pytest.mark.asyncio
    async def test_drift_record_exception_is_logged_not_raised(self):
        orch, _executor = self._make_orch_with_position()
        orch._drift_adapter.record_closed_trade = AsyncMock(side_effect=RuntimeError("boom"))

        async def _fake_sleep(_s):
            orch._running = False

        with (
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch(
                "src.engine.orchestrator.runtime_config.get_risk_controls",
                new_callable=AsyncMock,
                return_value=self._CONTROLS,
            ),
        ):
            await orch._position_monitor_loop()  # must not raise

    @pytest.mark.asyncio
    async def test_no_drift_detector_skips_record(self):
        orch, _executor = self._make_orch_with_position()
        orch._drift_detector = None

        async def _fake_sleep(_s):
            orch._running = False

        with (
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch(
                "src.engine.orchestrator.runtime_config.get_risk_controls",
                new_callable=AsyncMock,
                return_value=self._CONTROLS,
            ),
        ):
            await orch._position_monitor_loop()

        orch._drift_adapter.record_closed_trade.assert_not_awaited()


# ---------------------------------------------------------------------------
# _tick() — scheduled retraining at the periodic interval
# ---------------------------------------------------------------------------


class TestTickScheduledRetrain:
    def _make_orch_at_interval(self, prior_task=None):
        from src.engine.orchestrator import _RETRAIN_INTERVAL_TICKS

        orch = _make_orch()
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor
        orch._storage = _make_storage()
        # tick_counts is incremented before the modulo check, so start one below
        # the interval so post-increment it lands exactly on the boundary.
        orch._tick_counts = {Timeframe.INTRADAY.value: _RETRAIN_INTERVAL_TICKS - 1}
        orch._last_tick_ts = {Timeframe.INTRADAY.value: 0.0}
        orch._last_close_for_corr = {}
        if prior_task is not None:
            orch._retrain_tasks[Timeframe.INTRADAY.value] = prior_task
        return orch, executor

    @pytest.mark.asyncio
    async def test_scheduled_retrain_fires_at_interval(self):
        orch, _executor = self._make_orch_at_interval()
        result = _make_skip_result()
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=result)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}

        train_called = []

        async def _fake_train(tf):
            train_called.append(tf)

        orch._train_models = _fake_train

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)
        tracker.push_bar_returns = MagicMock()

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
        ):
            await orch._tick(Timeframe.INTRADAY)
            await asyncio.sleep(0)  # let the fire-and-forget task run

        assert Timeframe.INTRADAY.value in [tf.value for tf in train_called] or train_called

    @pytest.mark.asyncio
    async def test_scheduled_retrain_skipped_when_already_running(self):
        prior = asyncio.get_event_loop().create_future()
        prior.done = MagicMock(return_value=False)  # simulate a still-running task
        orch, _executor = self._make_orch_at_interval(prior_task=prior)

        result = _make_skip_result()
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=result)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)
        tracker.push_bar_returns = MagicMock()

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
        ):
            await orch._tick(Timeframe.INTRADAY)

        # Prior task is still "running" → same task object must remain registered
        assert orch._retrain_tasks[Timeframe.INTRADAY.value] is prior
        prior.cancel()

    @pytest.mark.asyncio
    async def test_scheduled_retrain_done_callback_skips_del_when_task_already_replaced(self):
        orch, _executor = self._make_orch_at_interval()
        result = _make_skip_result()
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=result)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}

        async def _fast_train(tf):
            return None

        orch._train_models = _fast_train

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)
        tracker.push_bar_returns = MagicMock()

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
        ):
            await orch._tick(Timeframe.INTRADAY)
            original_task = orch._retrain_tasks[Timeframe.INTRADAY.value]
            decoy = asyncio.get_event_loop().create_future()
            orch._retrain_tasks[Timeframe.INTRADAY.value] = decoy  # type: ignore[assignment]
            await original_task

        assert orch._retrain_tasks[Timeframe.INTRADAY.value] is decoy
        decoy.cancel()

    @pytest.mark.asyncio
    async def test_scheduled_retrain_failure_records_last_error(self):
        """SCAN2-003: an exception from the periodic (non-drift) retrain task
        must be logged/recorded via its done-callback, not vanish silently."""
        orch, _executor = self._make_orch_at_interval()
        result = _make_skip_result()
        mock_engine = MagicMock()
        mock_engine.tick = AsyncMock(return_value=result)
        orch._engines = {Timeframe.INTRADAY.value: mock_engine}

        async def _failing_train(tf):
            raise RuntimeError("scheduled retrain blew up")

        orch._train_models = _failing_train

        tracker = MagicMock()
        tracker.correlation_scalar = MagicMock(return_value=1.0)
        tracker.push_bar_returns = MagicMock()

        with (
            patch("src.engine.orchestrator.get_portfolio_correlation", return_value=tracker),
            patch(
                "src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)
            ),
        ):
            await orch._tick(Timeframe.INTRADAY)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert "scheduled retrain blew up" in orch._last_retrain_error[Timeframe.INTRADAY.value]
        assert Timeframe.INTRADAY.value not in orch._retrain_tasks


# ---------------------------------------------------------------------------
# _train_models() — feature build ValueError, HMM exception, hot-swap
# ---------------------------------------------------------------------------


class TestTrainModelsRemainingBranches:
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
    async def test_feature_build_value_error_returns_early(self):
        orch = self._orch_for_train()
        with patch(
            "src.engine.orchestrator.build_feature_matrix",
            side_effect=ValueError("bad bars"),
        ):
            await orch._train_models(Timeframe.INTRADAY)  # must not raise
        assert Timeframe.INTRADAY.value not in orch._trainers

    @pytest.mark.asyncio
    async def test_hmm_train_exception_continues_to_xgb(self):
        orch = self._orch_for_train()
        fm = MagicMock()
        fm.features = MagicMock()
        dir_result = MagicMock()
        dir_result.oos_sharpe = 1.0
        dir_result.live_gate_pass = False
        dir_result.model = MagicMock()
        dir_result.to_metrics_record = MagicMock(return_value=MagicMock())
        meta_result = MagicMock()
        meta_result.oos_sharpe = 0.9
        meta_result.live_gate_pass = False
        meta_result.to_metrics_record = MagicMock(return_value=MagicMock())
        trainer = MagicMock()
        trainer.train_direction = MagicMock(return_value=dir_result)
        trainer.train_meta_label = MagicMock(return_value=meta_result)
        trainer.save = MagicMock()
        detector = MagicMock()
        detector.fit = MagicMock(side_effect=RuntimeError("hmm blew up"))
        detector.save = MagicMock()

        with (
            patch("src.engine.orchestrator.build_feature_matrix", return_value=fm),
            patch("src.engine.orchestrator.ModelTrainer", return_value=trainer),
            patch("src.engine.orchestrator.RegimeDetector", return_value=detector),
        ):
            await orch._train_models(Timeframe.INTRADAY)  # must not raise

        # HMM failed → detector never registered, but XGB training still ran
        assert Timeframe.INTRADAY.value not in orch._detectors
        assert Timeframe.INTRADAY.value in orch._trainers

    @pytest.mark.asyncio
    async def test_hot_swap_models_called_when_engine_exists(self):
        orch = self._orch_for_train()
        existing_engine = AsyncMock()
        existing_engine.swap_models = AsyncMock(return_value=None)
        orch._engines[Timeframe.INTRADAY.value] = existing_engine

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

        new_dir = MagicMock()
        new_meta = MagicMock()

        with (
            patch("src.engine.orchestrator.build_feature_matrix", return_value=fm),
            patch("src.engine.orchestrator.ModelTrainer", return_value=trainer),
            patch("src.engine.orchestrator.RegimeDetector", return_value=detector),
            patch("src.engine.orchestrator.ModelTrainer.load_direction", return_value=new_dir),
            patch("src.engine.orchestrator.ModelTrainer.load_meta", return_value=new_meta),
        ):
            await orch._train_models(Timeframe.INTRADAY)

        existing_engine.swap_models.assert_awaited_once_with(
            new_dir, new_meta, detector, ensemble=trainer.train_ensemble.return_value
        )

    @pytest.mark.asyncio
    async def test_ensemble_train_exception_falls_back_to_none_and_still_swaps(self):
        """train_ensemble()/save_ensemble() failing must not block the
        direction/meta models -- which already trained/saved successfully --
        from being hot-swapped in with ensemble=None."""
        orch = self._orch_for_train()
        existing_engine = AsyncMock()
        existing_engine.swap_models = AsyncMock(return_value=None)
        orch._engines[Timeframe.INTRADAY.value] = existing_engine

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
        trainer.train_ensemble = MagicMock(side_effect=RuntimeError("ensemble blew up"))
        trainer.save = MagicMock()
        detector = MagicMock()
        detector.fit = MagicMock()
        detector.save = MagicMock()

        new_dir = MagicMock()
        new_meta = MagicMock()

        with (
            patch("src.engine.orchestrator.build_feature_matrix", return_value=fm),
            patch("src.engine.orchestrator.ModelTrainer", return_value=trainer),
            patch("src.engine.orchestrator.RegimeDetector", return_value=detector),
            patch("src.engine.orchestrator.ModelTrainer.load_direction", return_value=new_dir),
            patch("src.engine.orchestrator.ModelTrainer.load_meta", return_value=new_meta),
        ):
            await orch._train_models(Timeframe.INTRADAY)  # must not raise

        existing_engine.swap_models.assert_awaited_once_with(
            new_dir, new_meta, detector, ensemble=None
        )


# ---------------------------------------------------------------------------
# _position_monitor_loop() — remaining branches
# ---------------------------------------------------------------------------


class TestPositionMonitorRemainingBranches:
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
            cfg.risk.position_monitor_interval_s = 5
            mock_cfg.return_value = cfg
            orch = Orchestrator(storage, fetcher)
        return orch

    @pytest.mark.asyncio
    async def test_price_fetch_exception_continues_loop(self):
        orch = self._make_orch()
        orch._running = True
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(
            return_value=[{"symbol": "BTC/USDT", "trade_id": "t1"}]
        )
        orch._executor = executor
        orch._fetcher.fetch_ticker_price = AsyncMock(side_effect=RuntimeError("timeout"))

        iteration = 0

        async def _fake_sleep(_s):
            nonlocal iteration
            iteration += 1
            orch._running = False

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await orch._position_monitor_loop()  # must not raise

        executor.mark_to_market.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_position_with_different_symbol_is_skipped(self):
        orch = self._make_orch()
        orch._running = True
        executor = _make_executor()
        other_symbol_pos = {
            "symbol": "ETH/USDT",  # not orch._symbol
            "trade_id": "t9",
            "unrealized_pnl_pct": -0.5,
            "entry_ts": 1_000_000,
        }
        executor.open_positions_safe = AsyncMock(return_value=[other_symbol_pos])
        executor.mark_to_market = AsyncMock(return_value=None)
        executor.close_position = AsyncMock(return_value=0.0)
        orch._executor = executor

        controls = {
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.05,
            "take_profit_enabled": False,
            "take_profit_pct": 0.1,
            "max_holding_period_s": 86400.0,
        }

        async def _fake_sleep(_s):
            orch._running = False

        with (
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch(
                "src.engine.orchestrator.runtime_config.get_risk_controls",
                new_callable=AsyncMock,
                return_value=controls,
            ),
        ):
            await orch._position_monitor_loop()

        executor.close_position.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exit_reason_none_skips_close(self):
        orch = self._make_orch()
        orch._running = True
        executor = _make_executor()
        pos = {
            "symbol": "BTC/USDT",
            "trade_id": "t1",
            "unrealized_pnl_pct": 0.01,
            "entry_ts": 1_000_000,
        }
        executor.open_positions_safe = AsyncMock(return_value=[pos])
        executor.mark_to_market = AsyncMock(return_value=None)
        executor.close_position = AsyncMock(return_value=0.0)
        orch._executor = executor

        controls = {
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.05,
            "take_profit_enabled": False,
            "take_profit_pct": 0.1,
            "max_holding_period_s": 86400.0,
        }

        async def _fake_sleep(_s):
            orch._running = False

        with (
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch(
                "src.engine.orchestrator.runtime_config.get_risk_controls",
                new_callable=AsyncMock,
                return_value=controls,
            ),
            patch("src.engine.orchestrator.check_position_exit", return_value=None),
        ):
            await orch._position_monitor_loop()

        executor.close_position.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_close_position_keyerror_logged_as_debug(self):
        orch = self._make_orch()
        orch._running = True
        executor = _make_executor()
        pos = {
            "symbol": "BTC/USDT",
            "trade_id": "t1",
            "unrealized_pnl_pct": -0.06,
            "entry_ts": 1_000_000,
        }
        executor.open_positions_safe = AsyncMock(return_value=[pos])
        executor.mark_to_market = AsyncMock(return_value=None)
        executor.close_position = AsyncMock(side_effect=KeyError("already closed"))
        orch._executor = executor

        controls = {
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.05,
            "take_profit_enabled": False,
            "take_profit_pct": 0.1,
            "max_holding_period_s": 86400.0,
        }

        async def _fake_sleep(_s):
            orch._running = False

        with (
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch(
                "src.engine.orchestrator.runtime_config.get_risk_controls",
                new_callable=AsyncMock,
                return_value=controls,
            ),
            patch("src.engine.orchestrator.check_position_exit", return_value="stop_loss"),
        ):
            await orch._position_monitor_loop()  # must not raise

    @pytest.mark.asyncio
    async def test_close_position_generic_exception_logged(self):
        orch = self._make_orch()
        orch._running = True
        executor = _make_executor()
        pos = {
            "symbol": "BTC/USDT",
            "trade_id": "t1",
            "unrealized_pnl_pct": -0.06,
            "entry_ts": 1_000_000,
        }
        executor.open_positions_safe = AsyncMock(return_value=[pos])
        executor.mark_to_market = AsyncMock(return_value=None)
        executor.close_position = AsyncMock(side_effect=RuntimeError("db write failed"))
        orch._executor = executor

        controls = {
            "stop_loss_enabled": True,
            "stop_loss_pct": 0.05,
            "take_profit_enabled": False,
            "take_profit_pct": 0.1,
            "max_holding_period_s": 86400.0,
        }

        async def _fake_sleep(_s):
            orch._running = False

        with (
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch(
                "src.engine.orchestrator.runtime_config.get_risk_controls",
                new_callable=AsyncMock,
                return_value=controls,
            ),
            patch("src.engine.orchestrator.check_position_exit", return_value="stop_loss"),
        ):
            await orch._position_monitor_loop()  # must not raise

    @pytest.mark.asyncio
    async def test_outer_loop_exception_retries_after_5s(self):
        orch = self._make_orch()
        orch._running = True
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(side_effect=RuntimeError("storage down"))
        orch._executor = executor

        iteration = 0

        async def _fake_sleep(_s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                orch._running = False

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await orch._position_monitor_loop()

        assert iteration == 2  # first sleep(interval) + retry sleep(5)

    @pytest.mark.asyncio
    async def test_no_executor_continue_branch(self):
        """orch._running must actually be True for the loop body to execute at all."""
        orch = self._make_orch()
        orch._running = True
        orch._executor = None

        iteration = 0

        async def _fake_sleep(_s):
            nonlocal iteration
            iteration += 1
            orch._running = False

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await orch._position_monitor_loop()

        assert iteration == 1
        orch._fetcher.fetch_ticker_price.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_positions_continue_branch(self):
        orch = self._make_orch()
        orch._running = True
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(return_value=[])
        orch._executor = executor

        async def _fake_sleep(_s):
            orch._running = False

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await orch._position_monitor_loop()

        orch._fetcher.fetch_ticker_price.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_price_zero_continue_branch(self):
        orch = self._make_orch()
        orch._running = True
        executor = _make_executor()
        executor.open_positions_safe = AsyncMock(
            return_value=[{"symbol": "BTC/USDT", "trade_id": "t1"}]
        )
        executor.mark_to_market = AsyncMock()
        orch._executor = executor
        orch._fetcher.fetch_ticker_price = AsyncMock(return_value=0.0)

        async def _fake_sleep(_s):
            orch._running = False

        with patch("asyncio.sleep", side_effect=_fake_sleep):
            await orch._position_monitor_loop()

        executor.mark_to_market.assert_not_awaited()


# ---------------------------------------------------------------------------
# _timeframe_loop() — a full successful tick through the loop
# ---------------------------------------------------------------------------


class TestTimeframeLoopSuccessfulTick:
    @pytest.mark.asyncio
    async def test_loop_calls_tick_once_then_stops(self):
        orch = _make_orch()
        orch._running = True
        tick_calls = []

        async def _fake_tick(tf):
            tick_calls.append(tf)
            orch._running = False

        with (
            patch.object(orch, "_sleep_until_next_bar", new=AsyncMock(return_value=None)),
            patch.object(orch, "_tick", new=_fake_tick),
        ):
            await orch._timeframe_loop(Timeframe.INTRADAY)

        assert tick_calls == [Timeframe.INTRADAY]
