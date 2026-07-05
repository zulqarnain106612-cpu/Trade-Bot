"""
Orchestrator — top-level async event loop coordinating all subsystems.

Responsibilities:
  - Bootstrap history and train initial models on startup
  - Schedule per-timeframe ticks aligned to bar close times
  - Route tradeable signals to the correct executor (paper / live)
  - Snapshot regime state to storage after each tick
  - Schedule periodic model retraining
  - Reset daily equity tracker at UTC midnight
  - Expose stop() for clean shutdown from API

Authority:
  - Chan (2013) Algorithmic Trading Ch.5 — event-driven architecture
  - López de Prado (2018) AFML Ch.17 — regime-gated execution
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Union

import pandas as pd  # SCAN3-006: moved from inline imports inside _train_models()
import structlog

from src.api.metrics import update_metrics
from src.config import (
    TIMEFRAME_SECONDS,
    Timeframe,
    TradingMode,
    get_settings,
    runtime_config,
)
from src.data.fetcher import MarketDataFetcher
from src.data.storage import RegimeSnapshotRecord, StorageBackend
from src.diagnostics.runtime_monitor import get_monitor
from src.diagnostics.signal_debugger import (
    run_pipeline_selftest,
)
from src.engine.signal_engine import SignalEngine, SignalResult
from src.execution.live import LiveExecutor
from src.execution.paper import PaperExecutor
from src.features.pipeline import build_feature_matrix
from src.models.trainer import ModelTrainer
from src.regime.detector import RegimeDetector
from src.risk.drift_integration import DriftIntegrationAdapter
from src.risk.gates import check_position_exit
from src.risk.kelly import compute_win_loss_stats
from src.risk.performance_drift import PerformanceBaseline, PerformanceDriftDetector
from src.risk.portfolio_correlation import get_portfolio_correlation


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Retrain every N ticks of the primary timeframe (≈ daily for 15m bars)
_RETRAIN_INTERVAL_TICKS: int = 96  # 96 × 15m = 24 h
_HISTORY_BARS_FOR_TRAIN: int = 2000
_REGIME_LOOKBACK_BARS: int = 500

AnyExecutor = Union[PaperExecutor, LiveExecutor]


class Orchestrator:
    """
    Top-level async coordinator for the trading bot.

    Usage::

        orch = Orchestrator(storage, fetcher)
        await orch.startup()
        await orch.run()          # blocks until stop() called
        await orch.shutdown()

    Or use the FastAPI lifespan handler which calls startup/shutdown.
    """

    def __init__(
        self,
        storage: StorageBackend,
        fetcher: MarketDataFetcher,
    ) -> None:
        self._storage = storage
        self._fetcher = fetcher
        self._cfg = get_settings()
        self._symbol = self._cfg.primary_symbol
        self._timeframes = self._cfg.active_timeframes
        self._primary_tf = self._cfg.primary_timeframe

        self._executor: AnyExecutor | None = None
        self._engines: dict[str, SignalEngine] = {}
        self._detectors: dict[str, RegimeDetector] = {}
        self._trainers: dict[str, ModelTrainer] = {}
        # Track active retrain tasks per timeframe — prevents overlapping retrains
        self._retrain_tasks: dict[str, asyncio.Task] = {}
        # SCAN2-003: track last retrain error per timeframe so /status can surface it
        self._last_retrain_error: dict[str, str] = {}

        # GAP-003: Performance drift detector (initialized in startup after models trained)
        self._drift_detector: PerformanceDriftDetector | None = None
        self._drift_adapter = DriftIntegrationAdapter(self._drift_detector)

        # Dedicated single-thread executor for CPU-bound training (NEW-002).
        # Isolated from the default pool so training never starves async I/O tasks.
        self._train_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="training"
        )

        self._running: bool = False
        self._tick_counts: dict[str, int] = {tf.value: 0 for tf in self._timeframes}
        self._last_tick_ts: dict[str, float] = {tf.value: 0.0 for tf in self._timeframes}
        self._stop_event: asyncio.Event = asyncio.Event()
        self._log = log.bind(component="orchestrator", symbol=self._symbol)

        # GAP-005/GAP-015: last-seen (ts, close) per timeframe — used to compute
        # a simple bar return each tick to feed the shared PortfolioCorrelationTracker
        # singleton (src.risk.portfolio_correlation). Keyed by timeframe since this
        # Orchestrator instance is single-symbol (self._symbol); the tracker itself
        # is process-wide and aggregates returns pushed by every symbol's Orchestrator.
        self._last_close_for_corr: dict[str, tuple[int, float]] = {}

    # ------------------------------------------------------------------
    # Startup — bootstrap all subsystems
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """
        Initialize all subsystems in order:
          1. Executor (paper or live)
          2. Bootstrap historical bars for all timeframes
          3. Train initial models for all timeframes
          4. Build signal engines
        """
        self._log.info("orchestrator.startup", trading_mode=self._cfg.trading_mode.value)

        # Create executor
        if self._cfg.trading_mode == TradingMode.LIVE:
            self._executor = LiveExecutor(
                self._storage,
                self._fetcher,
                starting_capital=self._cfg.starting_capital_usd,
            )
        else:
            self._executor = PaperExecutor(
                self._storage,
                starting_capital=self._cfg.starting_capital_usd,
            )
        await self._executor.initialize()

        # ── Patch A: Runtime monitor + pipeline self-test ─────────────────
        _mon = get_monitor()
        _mon.register_probe("storage", self._storage.health_check)
        for _tf in self._timeframes:
            _tf_val = _tf.value
            _mon.register_tick_source(
                _tf_val,
                lambda _k=_tf_val: self._last_tick_ts.get(_k, 0.0),
            )
        await _mon.start()
        _selftest = run_pipeline_selftest()
        if not _selftest["passed"]:
            raise RuntimeError(
                f"Pipeline self-test FAILED on startup: {_selftest['error']}"
            )
        self._log.info("orchestrator.selftest_passed", rows=_selftest.get("n_rows"))
        # ──────────────────────────────────────────────────────────────────

        # Bootstrap bars — serialized (semaphore=1) to avoid exchange rate-limit bans.
        # Concurrent bootstrap across 3 timeframes generates burst fetches that exceed
        # Binance's 1200 req/min weight limit on cold start.
        bootstrap_sem = asyncio.Semaphore(1)

        async def _bootstrap_one(tf: Timeframe) -> int:
            async with bootstrap_sem:
                return await self._fetcher.bootstrap_history(self._symbol, tf)

        bootstrap_tasks = [_bootstrap_one(tf) for tf in self._timeframes]
        results = await asyncio.gather(*bootstrap_tasks, return_exceptions=True)

        # Fail loudly on bootstrap errors — never silently continue with no data (fix #15)
        for tf, result in zip(self._timeframes, results, strict=True):
            if isinstance(result, BaseException):
                self._log.critical(
                    "orchestrator.bootstrap_failed",
                    timeframe=tf.value,
                    error=str(result),
                )
                raise RuntimeError(
                    f"Bootstrap failed for timeframe {tf.value}: {result}"
                ) from result

        # Train models for each timeframe
        for tf in self._timeframes:
            await self._train_models(tf)

        # Initialize drift detector with baseline from trained models (GAP-003)
        # Extract OOS Sharpe, accuracy, win rate, max DD from primary model
        if self._primary_tf in self._trainers:
            trainer = self._trainers[self._primary_tf]
            # Attempt to read OOS metrics from trainer's internal cache
            # If available, create baseline; otherwise skip drift detection
            try:
                baseline = PerformanceBaseline(
                    train_sharpe=getattr(trainer, "train_sharpe", 2.0),
                    oos_sharpe=getattr(trainer, "oos_sharpe", 1.5),
                    train_accuracy=getattr(trainer, "train_accuracy", 0.60),
                    oos_accuracy=getattr(trainer, "oos_accuracy", 0.58),
                    train_win_rate=getattr(trainer, "train_win_rate", 0.55),
                    max_drawdown_pct=getattr(trainer, "max_drawdown_pct", 0.10),
                    trades_in_backtest=getattr(trainer, "trades_count", 400),
                )
                self._drift_detector = PerformanceDriftDetector(baseline)
                self._drift_adapter = DriftIntegrationAdapter(self._drift_detector)
                self._log.info(
                    "orchestrator.drift_detector_initialized",
                    baseline_sharpe=baseline.oos_sharpe,
                    baseline_accuracy=baseline.oos_accuracy,
                )
            except Exception as exc:
                self._log.warning(
                    "orchestrator.drift_detector_init_failed",
                    error=str(exc),
                    action="continuing_without_drift_detection",
                )

        # Build signal engines
        model_dir = self._cfg.storage.model_dir
        for tf in self._timeframes:
            detector = self._detectors.get(tf.value)
            trainer = self._trainers.get(tf.value)
            if detector is None or trainer is None:
                self._log.warning("orchestrator.engine_skip", timeframe=tf.value)
                continue

            try:
                direction_model = ModelTrainer.load_direction(model_dir, self._symbol, tf.value)
                meta_model = ModelTrainer.load_meta(model_dir, self._symbol, tf.value)
            except FileNotFoundError:
                self._log.warning("orchestrator.models_not_found", timeframe=tf.value)
                continue

            self._engines[tf.value] = SignalEngine(
                symbol=self._symbol,
                timeframe=tf,
                storage=self._storage,
                fetcher=self._fetcher,
                detector=detector,
                direction_model=direction_model,
                meta_model=meta_model,
                trainer=trainer,
            )

        self._log.info(
            "orchestrator.startup_complete",
            engines=list(self._engines.keys()),
        )

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Run the main trading loop until stop() is called.

        Spawns one async task per active timeframe, each sleeping until
        the next bar close then triggering a tick.
        """
        self._running = True
        self._log.info("orchestrator.run_start")

        tasks = [
            asyncio.create_task(self._timeframe_loop(tf), name=f"loop_{tf.value}")
            for tf in self._timeframes
            if tf.value in self._engines
        ]
        tasks.append(asyncio.create_task(self._midnight_reset_loop(), name="midnight_reset"))
        tasks.append(asyncio.create_task(self._position_monitor_loop(), name="position_monitor"))

        # Wait until stop event
        await self._stop_event.wait()
        self._running = False

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._log.info("orchestrator.run_stopped")

    def stop(self) -> None:
        """Signal the run loop to exit cleanly."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Flush state, close all subsystems, and shut down training executor."""
        if self._executor is not None:
            await self._executor.shutdown()
        await get_monitor().stop()  # Patch B: clean monitor shutdown
        # Shut down training thread pool cleanly — wait for any in-flight training job
        self._train_executor.shutdown(wait=True)
        self._log.info("orchestrator.shutdown_complete")

    # ------------------------------------------------------------------
    # Timeframe loop
    # ------------------------------------------------------------------

    async def _timeframe_loop(self, tf: Timeframe) -> None:
        """
        Async loop for a single timeframe.

        Sleeps until the next bar close (aligned to bar boundary),
        then fires a tick.
        """
        tf_seconds = TIMEFRAME_SECONDS[tf]
        self._log.info("orchestrator.tf_loop_start", timeframe=tf.value)

        while self._running:
            try:
                await self._sleep_until_next_bar(tf_seconds)
                if not self._running:
                    break
                await self._tick(tf)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error(
                    "orchestrator.tf_loop_error",
                    timeframe=tf.value,
                    error=str(exc),
                )
                # Back off on repeated errors to avoid tight error loop
                await asyncio.sleep(min(tf_seconds, 30))

    async def _sleep_until_next_bar(self, tf_seconds: int) -> None:
        """Sleep until the next bar boundary (UTC aligned)."""
        now = datetime.now(tz=UTC).timestamp()
        next_bar = (int(now / tf_seconds) + 1) * tf_seconds
        sleep_s = max(0.1, next_bar - now)
        await asyncio.sleep(sleep_s)

    # ------------------------------------------------------------------
    # Tick — one bar cycle for a single timeframe
    # ------------------------------------------------------------------

    async def _tick(self, tf: Timeframe) -> None:
        """Execute one signal cycle for the given timeframe."""
        engine = self._engines.get(tf.value)
        if engine is None:
            return

        executor = self._executor
        if executor is None:
            return

        self._tick_counts[tf.value] += 1
        self._last_tick_ts[tf.value] = time.monotonic()

        # Gather risk context from executor + storage
        daily_pnl = await executor.get_daily_pnl(self._symbol)
        consecutive_losses = await executor.get_consecutive_losses(self._symbol)
        capital_usd = executor.equity_usd
        starting_equity = executor.starting_equity_usd  # NEW-009: via AbstractExecutor property

        # Live gate status from storage
        direction_gate = False
        meta_gate = False
        dir_metrics = await self._storage.latest_model_metrics("direction", tf.value)
        meta_metrics = await self._storage.latest_model_metrics("meta_label", tf.value)
        if dir_metrics is not None:
            direction_gate = dir_metrics.live_gate_pass
        if meta_metrics is not None:
            meta_gate = meta_metrics.live_gate_pass

        # Win/loss stats for Kelly
        recent_trades = await self._storage.fetch_trades(
            symbol=self._symbol,
            trading_mode=self._cfg.trading_mode.value,
            limit=200,  # raised from 100 (VUL-020: larger window reduces overfit sizing)
        )
        # SCAN2-012: fetch_trades returns DESC order (most-recent first). Reverse to
        # chronological order before passing to compute_win_loss_stats so any
        # future time-order-sensitive analysis gets correct sequencing.
        pnl_history = [
            t.pnl_usd for t in reversed(recent_trades) if t.pnl_usd is not None
        ]
        _, avg_win, avg_loss = compute_win_loss_stats(pnl_history)

        # Paper trading tenure — days since first paper equity record.
        # Passed to the gate stack so check_paper_minimum_days is enforced
        # when TRADING_MODE=live (VUL-034).
        paper_trading_days = 0
        if self._cfg.trading_mode.value == "live":
            earliest = await self._storage.earliest_equity_ts(TradingMode.PAPER.value)
            if earliest is not None:
                age_s = datetime.now(tz=UTC).timestamp() - (earliest / 1000.0)
                paper_trading_days = int(age_s / 86400)

        # GAP-005/GAP-015: push this symbol's bar return into the shared
        # portfolio-correlation tracker, then compute a sizing scalar from
        # this symbol's correlation with all OTHER currently-open positions
        # (excludes self._symbol's own position, if any — correlating a
        # symbol with itself is meaningless and would always read as 1.0).
        # Fails safe to 1.0 (no-op, matches compute_position_size's default)
        # on any error so a tracker bug degrades to "no correlation
        # adjustment" rather than blocking sizing entirely.
        correlation_scalar = 1.0
        try:
            tracker = get_portfolio_correlation()
            latest = await self._storage.latest_close(self._symbol, tf.value)
            if latest is not None:
                ts, close = latest
                prev = self._last_close_for_corr.get(tf.value)
                if prev is not None and prev[0] != ts and prev[1] > 0.0:
                    bar_return = (close - prev[1]) / prev[1]
                    tracker.push_bar_returns({self._symbol: bar_return})
                self._last_close_for_corr[tf.value] = (ts, close)

            open_positions = await executor.open_positions_safe()
            other_open_symbols = [
                p["symbol"] for p in open_positions
                if p.get("symbol") != self._symbol
            ]
            correlation_scalar = tracker.correlation_scalar(
                new_symbol=self._symbol,
                open_symbols=other_open_symbols,
            )
        except Exception as exc:
            self._log.error(
                "orchestrator.correlation_scalar_failed",
                error=str(exc),
                fallback_scalar=1.0,
            )
            correlation_scalar = 1.0

        # Run signal engine
        result: SignalResult = await engine.tick(
            capital_usd=capital_usd,
            daily_pnl_usd=daily_pnl,
            starting_equity_usd=starting_equity,
            consecutive_loss_count=consecutive_losses,
            direction_gate_pass=direction_gate,
            meta_gate_pass=meta_gate,
            avg_win_usd=avg_win,
            avg_loss_usd=avg_loss,
            paper_trading_days=paper_trading_days,
            correlation_scalar=correlation_scalar,
        )

        # Persist regime snapshot
        if result.regime is not None:
            latest_ts = await self._storage.latest_bar_ts(self._symbol, tf.value) or 0
            snap = RegimeSnapshotRecord(
                symbol=self._symbol,
                timeframe=tf.value,
                ts=latest_ts,
                regime_state=result.regime.state,
                prob_ranging=result.regime.prob_ranging,
                prob_trending=result.regime.prob_trending,
                prob_volatile=result.regime.prob_volatile,
            )
            await self._storage.upsert_regime_snapshot(snap)

        # TASK-007: push metrics snapshot to Prometheus gauges/counters
        try:
            _executor = getattr(self, "_executor", None)
            update_metrics({
                "signal_score":   float(result.p_long - (1.0 - result.p_long)),
                "regime_state":   result.regime.state if result.regime else 0,
                "prob_ranging":   result.regime.prob_ranging if result.regime else 0.0,
                "prob_trending":  result.regime.prob_trending if result.regime else 0.0,
                "prob_volatile":  result.regime.prob_volatile if result.regime else 0.0,
                "kelly_fraction": result.kelly_result.adjusted_fraction if result.kelly_result else 0.0,
                "equity_usd":     _executor.equity_usd if _executor else 0.0,
                "open_positions": len(_executor.open_positions) if _executor else 0,
            })
        except Exception:
            pass  # metric failure must never affect trade path

        # Route to executor if tradeable
        if result.tradeable and result.kelly_result is not None:

            # Check Gate 6: Performance drift (GAP-003)
            drift_status = self._drift_adapter.check_drift()
            if drift_status.get("drifted"):
                self._log.warning(
                    "orchestrator.signal_blocked_drift",
                    timeframe=tf.value,
                    drift_metric=drift_status.get("metric"),
                    reason=drift_status.get("reason"),
                )
                return  # Skip this signal due to drift

            current_price = result.kelly_result.entry_price
            if current_price <= 0.0:
                # Fetch current price for fill simulation
                try:
                    current_price = await self._fetcher.fetch_ticker_price(self._symbol)
                except Exception:
                    # VUL-ORCHESTRATOR-001: previous fallback used capital_usd which is
                    # equity in USD, not an asset price — would produce nonsensical position
                    # sizes. Skip the signal instead; a bad price is worse than no trade.
                    self._log.error(
                        "orchestrator.signal_skip_no_price",
                        timeframe=tf.value,
                        reason="ticker_fetch_failed_and_no_known_price",
                    )
                    return

            trade_id, outcome = await executor.submit_signal(
                symbol=self._symbol,
                timeframe=tf.value,
                direction=result.direction,
                kelly_result=result.kelly_result,
                regime_state=result.regime.state if result.regime else 0,
                meta_label_prob=result.p_bet,
                raw_signal=result.p_long,
                current_price=current_price,
            )

            # Record trade outcome for drift detection (GAP-003)
            # Called after signal submission; outcome contains entry/exit prices
            if self._drift_detector and hasattr(outcome, "__dict__"):
                try:
                    # Extract P&L and prediction confidence from outcome
                    pnl = outcome.get("pnl_usd", 0.0) if hasattr(outcome, "get") else getattr(outcome, "pnl_usd", 0.0)
                    pred_prob = result.p_long or 0.5  # Direction model prediction
                    actual_dir = 1 if result.direction > 0 else -1
                    current_equity = await executor.get_current_equity()

                    await self._drift_adapter.record_closed_trade(
                        trade_id=trade_id,
                        exit_price=outcome.get("exit_price", 0.0) if hasattr(outcome, "get") else 0.0,
                        pnl_usd=pnl,
                        predicted_prob=pred_prob,
                        actual_direction=actual_dir,
                        current_equity=current_equity,
                        starting_equity=self._cfg.starting_capital_usd,
                    )
                except Exception as exc:
                    self._log.warning(
                        "orchestrator.drift_record_failed",
                        trade_id=trade_id,
                        error=str(exc),
                    )

            self._log.info(
                "orchestrator.signal_submitted",
                timeframe=tf.value,
                direction=result.direction,
                outcome=outcome,
                trade_id=trade_id,
            )

        # Scheduled retraining on primary timeframe — guard against overlap
        if tf == self._primary_tf and self._tick_counts[tf.value] % _RETRAIN_INTERVAL_TICKS == 0:
            prior = self._retrain_tasks.get(tf.value)
            if prior is None or prior.done():
                task = asyncio.create_task(
                    self._train_models(tf),
                    name=f"retrain_{tf.value}",
                )
                # SCAN2-003: log exceptions from the fire-and-forget retrain task;
                # without this, unhandled exceptions vanish silently until GC.
                def _retrain_done(t: asyncio.Task, _tf: str = tf.value) -> None:
                    if not t.cancelled() and t.exception() is not None:
                        err = str(t.exception())
                        self._last_retrain_error[_tf] = err
                        self._log.error(
                            "orchestrator.retrain_task_failed",
                            timeframe=_tf,
                            error=err,
                        )
                    # L-09: explicitly remove completed task to release references
                    # to training data arrays held in the task's result/exception.
                    if self._retrain_tasks.get(_tf) is t:
                        del self._retrain_tasks[_tf]
                task.add_done_callback(_retrain_done)
                self._retrain_tasks[tf.value] = task
            else:
                self._log.warning(
                    "orchestrator.retrain_skipped_already_running",
                    timeframe=tf.value,
                )

    # ------------------------------------------------------------------
    # Model training
    # ------------------------------------------------------------------

    async def _train_models(self, tf: Timeframe) -> None:
        """
        Train HMM + XGBoost models for a timeframe and persist to disk.

        Runs in the event loop — uses run_in_executor for CPU-bound steps
        so async tasks are not blocked.
        """
        self._log.info("orchestrator.training_start", timeframe=tf.value)
        loop = asyncio.get_event_loop()

        # Load bars
        # SCAN2-010: compute exact cutoff timestamp instead of since_ts=0 (full table scan).
        # Using the real cutoff makes the query use the (symbol, timeframe, ts) index optimally.
        tf_seconds = {
            "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400,
        }.get(tf.value, 60)
        cutoff_ts = int(
            (datetime.now(tz=UTC).timestamp() - _HISTORY_BARS_FOR_TRAIN * tf_seconds)
            * 1000
        )
        records = await self._storage.fetch_bars(
            self._symbol, tf.value, since_ts=cutoff_ts, limit=_HISTORY_BARS_FOR_TRAIN
        )
        if len(records) < 300:
            self._log.warning(
                "orchestrator.training_skip_insufficient_bars",
                n_bars=len(records),
                timeframe=tf.value,
            )
            return

        # SCAN3-006: pandas now at module level
        bars = pd.DataFrame(
            {
                "open": [r.open for r in records],
                "high": [r.high for r in records],
                "low": [r.low for r in records],
                "close": [r.close for r in records],
                "volume": [r.volume for r in records],
            },
            index=[r.ts for r in records],
        ).sort_index()

        # Feature matrix — CPU bound, run in dedicated training executor (NEW-002)
        try:
            fm = await loop.run_in_executor(self._train_executor, build_feature_matrix, bars)
        except ValueError as exc:
            self._log.error("orchestrator.feature_build_failed", error=str(exc))
            return

        # HMM training — CPU bound
        detector = RegimeDetector(self._symbol, tf.value)
        try:
            await loop.run_in_executor(self._train_executor, detector.fit, fm.features)
            detector.save(self._cfg.storage.model_dir)
            self._detectors[tf.value] = detector
        except Exception as exc:
            self._log.error("orchestrator.hmm_train_failed", error=str(exc))

        # XGBoost training — CPU bound
        trainer = ModelTrainer(self._symbol, tf.value)
        self._trainers[tf.value] = trainer
        version = datetime.now(tz=UTC).isoformat()

        try:
            dir_result = await loop.run_in_executor(
                self._train_executor, trainer.train_direction, fm
            )
            meta_result = await loop.run_in_executor(
                self._train_executor, trainer.train_meta_label, fm, dir_result.model
            )
            trainer.save(dir_result.model, meta_result.model, self._cfg.storage.model_dir, version)

            # Persist metrics to storage
            await self._storage.insert_model_metrics(
                dir_result.to_metrics_record("direction", tf.value, version)
            )
            await self._storage.insert_model_metrics(
                meta_result.to_metrics_record("meta_label", tf.value, version)
            )

            # Hot-swap models atomically via the engine's own lock (fix #14)
            if tf.value in self._engines:
                new_dir = ModelTrainer.load_direction(
                    self._cfg.storage.model_dir, self._symbol, tf.value
                )
                new_meta = ModelTrainer.load_meta(
                    self._cfg.storage.model_dir, self._symbol, tf.value
                )
                await self._engines[tf.value].swap_models(new_dir, new_meta, detector)

            self._log.info(
                "orchestrator.training_complete",
                timeframe=tf.value,
                oos_sharpe_dir=dir_result.oos_sharpe,
                oos_sharpe_meta=meta_result.oos_sharpe,
                dir_live_gate=dir_result.live_gate_pass,
                meta_live_gate=meta_result.live_gate_pass,
            )
        except Exception as exc:
            self._log.error("orchestrator.xgb_train_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Midnight reset
    # ------------------------------------------------------------------

    async def _midnight_reset_loop(self) -> None:
        """Reset daily equity tracker at UTC midnight."""
        while self._running:
            try:
                now = datetime.now(tz=UTC)
                # Seconds until next midnight
                next_midnight = (
                    now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() + 86400
                )
                sleep_s = max(1.0, next_midnight - now.timestamp())
                await asyncio.sleep(sleep_s)
                if self._executor is not None:
                    # NEW-005: atomic reset via executor method — avoids torn read
                    # during concurrent mark_to_market.
                    equity = await self._executor.reset_daily_equity()
                    self._log.info(
                        "orchestrator.daily_reset",
                        equity_usd=equity,
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error("orchestrator.midnight_reset_error", error=str(exc))
                await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # GAP-013 -- automated position-exit monitor
    # ------------------------------------------------------------------

    async def _position_monitor_loop(self) -> None:
        """
        Periodically mark open positions to market and close any that trip
        a stop-loss, take-profit, or max-holding-period condition.

        Runs on its own fast cadence (RiskSettings.position_monitor_interval_s,
        default 5s) independent of the per-timeframe signal-generation ticks
        (which may be 1h/4h/1d apart) -- a position opened by this system
        previously had no automated exit path at all (GAP-013); without this
        loop, mark_to_market() was only ever called from inside the executors
        themselves and close_position() was never called by anything in
        production, so a losing position could drift unbounded between
        signal ticks with no automatic exit.

        Stop-loss / take-profit are runtime-toggleable via
        RuntimeConfig.set_risk_controls() / POST /risk-controls so an
        operator can adjust or disable them without a redeploy. The
        max-holding-period time exit is always enforced (see
        check_position_exit's docstring for the rationale).
        """
        while self._running:
            try:
                await asyncio.sleep(self._cfg.risk.position_monitor_interval_s)
                if self._executor is None:
                    continue

                positions = await self._executor.open_positions_safe()
                if not positions:
                    continue

                try:
                    price = await self._fetcher.fetch_ticker_price(self._symbol)
                except Exception as exc:
                    self._log.warning("orchestrator.position_monitor_price_fetch_failed", error=str(exc))
                    continue
                if price <= 0.0:
                    continue

                await self._executor.mark_to_market({self._symbol: price})

                controls = await runtime_config.get_risk_controls()
                now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)

                # Re-snapshot after marking so unrealized_pnl_pct reflects the
                # price just fetched above, not a stale value from the last tick.
                positions = await self._executor.open_positions_safe()
                for pos in positions:
                    if pos.get("symbol") != self._symbol:
                        continue
                    exit_reason = check_position_exit(
                        unrealized_pnl_pct=float(pos["unrealized_pnl_pct"]),
                        entry_ts_ms=int(pos["entry_ts"]),
                        now_ts_ms=now_ms,
                        stop_loss_enabled=bool(controls["stop_loss_enabled"]),
                        stop_loss_pct=float(controls["stop_loss_pct"]),
                        take_profit_enabled=bool(controls["take_profit_enabled"]),
                        take_profit_pct=float(controls["take_profit_pct"]),
                        max_holding_period_s=float(controls["max_holding_period_s"]),
                    )
                    if exit_reason is None:
                        continue
                    try:
                        net_pnl = await self._executor.close_position(
                            trade_id=str(pos["trade_id"]),
                            exit_price=price,
                            exit_reason=exit_reason,
                        )
                        self._log.info(
                            "orchestrator.position_auto_closed",
                            trade_id=pos["trade_id"],
                            symbol=pos["symbol"],
                            exit_reason=exit_reason,
                            exit_price=price,
                            net_pnl_usd=round(net_pnl, 4),
                            unrealized_pnl_pct_at_close=pos["unrealized_pnl_pct"],
                        )
                    except KeyError:
                        # Position was already closed by another path (e.g. a manual
                        # close via the API) between the snapshot above and this call --
                        # not an error, just a race that resolved itself.
                        self._log.debug(
                            "orchestrator.position_monitor_already_closed",
                            trade_id=pos["trade_id"],
                        )
                    except Exception as exc:
                        self._log.error(
                            "orchestrator.position_auto_close_failed",
                            trade_id=pos["trade_id"],
                            exit_reason=exit_reason,
                            error=str(exc),
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error("orchestrator.position_monitor_loop_error", error=str(exc))
                await asyncio.sleep(5)
