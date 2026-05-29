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
from datetime import datetime, timezone
from typing import Union

import structlog

from src.config import (
    TIMEFRAME_SECONDS,
    Timeframe,
    TradingMode,
    get_settings,
)
from src.data.fetcher import MarketDataFetcher
from src.data.storage import RegimeSnapshotRecord, StorageBackend
from src.execution.paper import PaperExecutor
from src.execution.live import LiveExecutor
from src.engine.signal_engine import SignalEngine, SignalResult
from src.features.pipeline import build_feature_matrix
from src.models.trainer import ModelTrainer
from src.regime.detector import RegimeDetector
from src.risk.kelly import compute_win_loss_stats

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Retrain every N ticks of the primary timeframe (≈ daily for 15m bars)
_RETRAIN_INTERVAL_TICKS: int = 96   # 96 × 15m = 24 h
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

        self._running: bool = False
        self._tick_counts: dict[str, int] = {tf.value: 0 for tf in self._timeframes}
        self._stop_event: asyncio.Event = asyncio.Event()
        self._log = log.bind(component="orchestrator", symbol=self._symbol)

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
                self._storage, self._fetcher,
                starting_capital=self._cfg.starting_capital_usd,
            )
        else:
            self._executor = PaperExecutor(
                self._storage,
                starting_capital=self._cfg.starting_capital_usd,
            )
        await self._executor.initialize()

        # Bootstrap bars for all timeframes concurrently
        bootstrap_tasks = [
            self._fetcher.bootstrap_history(self._symbol, tf)
            for tf in self._timeframes
        ]
        await asyncio.gather(*bootstrap_tasks, return_exceptions=True)

        # Train models for each timeframe
        for tf in self._timeframes:
            await self._train_models(tf)

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
        """Flush state and close all subsystems."""
        if self._executor is not None:
            await self._executor.shutdown()
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
        now = datetime.now(tz=timezone.utc).timestamp()
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

        # Gather risk context from executor + storage
        daily_pnl = await executor.get_daily_pnl(self._symbol)
        consecutive_losses = await executor.get_consecutive_losses(self._symbol)
        capital_usd = executor.equity_usd
        starting_equity = executor.drawdown_tracker.daily_start_equity

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
            limit=100,
        )
        pnl_history = [t.pnl_usd for t in recent_trades if t.pnl_usd is not None]
        _, avg_win, avg_loss = compute_win_loss_stats(pnl_history)

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

        # Route to executor if tradeable
        if result.tradeable and result.kelly_result is not None:
            current_price = result.kelly_result.entry_price
            if current_price <= 0.0:
                # Fetch current price for fill simulation
                try:
                    current_price = await self._fetcher.fetch_ticker_price(self._symbol)
                except Exception:
                    current_price = result.kelly_result.capital_usd  # fallback

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
            self._log.info(
                "orchestrator.signal_submitted",
                timeframe=tf.value,
                direction=result.direction,
                outcome=outcome,
                trade_id=trade_id,
            )

        # Scheduled retraining on primary timeframe
        if (
            tf == self._primary_tf
            and self._tick_counts[tf.value] % _RETRAIN_INTERVAL_TICKS == 0
        ):
            asyncio.create_task(
                self._train_models(tf),
                name=f"retrain_{tf.value}",
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
        records = await self._storage.fetch_bars(
            self._symbol, tf.value, since_ts=0, limit=_HISTORY_BARS_FOR_TRAIN
        )
        if len(records) < 300:
            self._log.warning(
                "orchestrator.training_skip_insufficient_bars",
                n_bars=len(records),
                timeframe=tf.value,
            )
            return

        import pandas as pd
        import numpy as np
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

        # Feature matrix — CPU bound, run in executor
        try:
            fm = await loop.run_in_executor(None, build_feature_matrix, bars)
        except ValueError as exc:
            self._log.error("orchestrator.feature_build_failed", error=str(exc))
            return

        # HMM training
        detector = RegimeDetector(self._symbol, tf.value)
        try:
            await loop.run_in_executor(None, detector.fit, fm.features)
            detector.save(self._cfg.storage.model_dir)
            self._detectors[tf.value] = detector
        except Exception as exc:
            self._log.error("orchestrator.hmm_train_failed", error=str(exc))

        # XGBoost training
        trainer = ModelTrainer(self._symbol, tf.value)
        self._trainers[tf.value] = trainer
        version = datetime.now(tz=timezone.utc).isoformat()

        try:
            dir_result = await loop.run_in_executor(None, trainer.train_direction, fm)
            meta_result = await loop.run_in_executor(
                None, trainer.train_meta_label, fm, dir_result.model
            )
            trainer.save(dir_result.model, meta_result.model,
                         self._cfg.storage.model_dir, version)

            # Persist metrics to storage
            await self._storage.insert_model_metrics(
                dir_result.to_metrics_record("direction", tf.value, version)
            )
            await self._storage.insert_model_metrics(
                meta_result.to_metrics_record("meta_label", tf.value, version)
            )

            # Hot-swap models in running engine without restart
            if tf.value in self._engines:
                new_dir = ModelTrainer.load_direction(
                    self._cfg.storage.model_dir, self._symbol, tf.value
                )
                new_meta = ModelTrainer.load_meta(
                    self._cfg.storage.model_dir, self._symbol, tf.value
                )
                engine = self._engines[tf.value]
                engine._direction_model = new_dir   # noqa: SLF001
                engine._meta_model = new_meta        # noqa: SLF001
                engine._detector = detector          # noqa: SLF001

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
                now = datetime.now(tz=timezone.utc)
                # Seconds until next midnight
                next_midnight = (
                    now.replace(hour=0, minute=0, second=0, microsecond=0)
                    .timestamp() + 86400
                )
                sleep_s = max(1.0, next_midnight - now.timestamp())
                await asyncio.sleep(sleep_s)
                if self._executor is not None:
                    self._executor.drawdown_tracker.reset_daily(
                        self._executor.equity_usd
                    )
                    self._log.info(
                        "orchestrator.daily_reset",
                        equity_usd=self._executor.equity_usd,
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error("orchestrator.midnight_reset_error", error=str(exc))
                await asyncio.sleep(60)