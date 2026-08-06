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
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace as _dc_replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd  # SCAN3-006: moved from inline imports inside _train_models()
import structlog

from src.api.metrics import update_metrics
from src.config import (
    EXCHANGE_BINANCE,
    TIMEFRAME_SECONDS,
    Timeframe,
    TradingMode,
    get_settings,
    runtime_config,
)
from src.data.fetcher import MarketDataFetcher
from src.data.provider_cache import get_provider_cache
from src.data.storage import (
    AnyStorageBackend,
    MissedTradeRecord,
    ModelMetricsRecord,
    RegimeSnapshotRecord,
)
from src.diagnostics.runtime_monitor import get_monitor
from src.diagnostics.signal_debugger import (
    run_pipeline_selftest,
)
from src.engine.crypto_box_adapter import CryptoBoxSignalAdapter
from src.engine.signal_engine import ShadowBundle, SignalEngine, SignalResult
from src.execution.live import LiveExecutor
from src.execution.paper import PaperExecutor
from src.execution.unified_ledger import VenuePosition, get_unified_ledger
from src.features.pipeline import build_feature_matrix
from src.intelligence.macro_indicators import build_macro_indicators
from src.intelligence.macro_regime import classify_macro_regime
from src.models.online_trainer import OnlineTrainer
from src.models.trainer import ModelTrainer
from src.regime.detector import RegimeDetector
from src.risk.drift_integration import DriftIntegrationAdapter
from src.risk.gates import check_position_exit
from src.risk.kelly import compute_win_loss_stats
from src.risk.macro_exposure_budget import (
    MacroExposureBudget,
    compute_macro_exposure_scalar,
)
from src.risk.performance_drift import PerformanceBaseline, PerformanceDriftDetector
from src.risk.portfolio_correlation import get_portfolio_correlation
from src.risk.strategy_correlation import (
    combined_correlation_scalar,
    get_strategy_correlation,
)
from src.risk.strategy_kill_switch import get_strategy_kill_switch_manager
from src.strategies.capital_allocator import performance_weighted_allocate
from src.strategies.registry import get_default_registry
from src.strategies.signal_engine_adapter import (
    STRATEGY_ID_SIGNAL_ENGINE,
    SignalEngineStrategy,
)
from src.tuning.meta_allocator import get_allocation_controller


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Retrain every N ticks of the primary timeframe (≈ daily for 15m bars)
_RETRAIN_INTERVAL_TICKS: int = 96  # 96 x 15m = 24 h
_HISTORY_BARS_FOR_TRAIN: int = 2000
_REGIME_LOOKBACK_BARS: int = 500

AnyExecutor = PaperExecutor | LiveExecutor


def _aggregate_venue_positions(
    venue: str, open_positions: list[dict[str, object]]
) -> list[VenuePosition]:
    """
    Collapse an executor's position dicts into one VenuePosition per symbol.

    UnifiedLedger is keyed by (venue, symbol), but one executor legitimately
    holds several positions in the same symbol — one per timeframe. Recording
    them individually would silently overwrite, leaving the ledger reporting
    whichever timeframe happened to be enumerated last. Netting the signed
    quantities is what a venue-level book actually means.

    entry_price is the gross-quantity-weighted average, so it stays a
    meaningful cost basis even when the legs disagree on direction; margin is
    summed over notional, because capital committed to a hedged pair is still
    committed.
    """
    signed_qty: dict[str, float] = {}
    gross_qty: dict[str, float] = {}
    notional: dict[str, float] = {}
    qty_price: dict[str, float] = {}

    for raw in open_positions:
        symbol = str(raw.get("symbol") or "")
        if not symbol:
            continue
        quantity = abs(float(cast("float", raw.get("quantity") or 0.0)))
        if quantity <= 0.0:
            continue
        direction = 1.0 if raw.get("direction") == "long" else -1.0
        price = float(cast("float", raw.get("entry_price") or 0.0))

        signed_qty[symbol] = signed_qty.get(symbol, 0.0) + direction * quantity
        gross_qty[symbol] = gross_qty.get(symbol, 0.0) + quantity
        notional[symbol] = notional.get(symbol, 0.0) + float(
            cast("float", raw.get("notional_usd") or 0.0)
        )
        qty_price[symbol] = qty_price.get(symbol, 0.0) + quantity * price

    return [
        VenuePosition(
            venue=venue,
            symbol=symbol,
            quantity=signed_qty[symbol],
            entry_price=qty_price[symbol] / gross_qty[symbol],
            margin_used_usd=notional[symbol],
        )
        for symbol in signed_qty
    ]


class Orchestrator:
    """
    Top-level async coordinator for the trading bot.

    Usage::

        orch = Orchestrator(storage, fetcher)
        await orch.startup()
        await orch.run()  # blocks until stop() called
        await orch.shutdown()

    Or use the FastAPI lifespan handler which calls startup/shutdown.
    """

    def __init__(
        self,
        storage: AnyStorageBackend,
        fetcher: MarketDataFetcher,
    ) -> None:
        self._storage = storage
        self._fetcher = fetcher
        self._cfg = get_settings()
        self._symbol = self._cfg.primary_symbol
        self._timeframes = self._cfg.active_timeframes
        self._primary_tf = self._cfg.primary_timeframe

        self._executor: AnyExecutor | None = None
        # Dedicated paper executor for non-primary timeframes when trading_mode=LIVE.
        # Spec (README): only primary_timeframe (intraday) trades real money;
        # scalping and swing streams are paper-only regardless of the global
        # trading mode. None when trading_mode=PAPER, since self._executor
        # already covers every timeframe in that case.
        self._non_primary_executor: PaperExecutor | None = None
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
        self._train_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="training")

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

        # Last mark-to-market unrealized P&L per trade_id, used to difference
        # one interval's return per strategy for StrategyCorrelationTracker.
        # Keyed by trade_id rather than strategy_id so a strategy running
        # several positions contributes each one's delta exactly once.
        self._last_unrealized_by_trade: dict[str, float] = {}

        # Crypto-Box 18-engine ensemble — activated only when CRYPTO_BOX=true.
        # Silently disabled otherwise, so the existing pipeline is unaffected.
        self._crypto_box = CryptoBoxSignalAdapter()

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
            # Non-primary timeframes (scalping, swing) never trade real money,
            # even when trading_mode=LIVE -- give them their own paper executor.
            if any(tf != self._primary_tf for tf in self._timeframes):
                self._non_primary_executor = PaperExecutor(
                    self._storage,
                    starting_capital=self._cfg.starting_capital_usd,
                )
                await self._non_primary_executor.initialize()
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

            def _make_ts_getter(k: str = _tf_val) -> Callable[[], float]:
                return lambda: self._last_tick_ts.get(k, 0.0)

            _mon.register_tick_source(_tf_val, _make_ts_getter())
        await _mon.start()
        _selftest = run_pipeline_selftest()
        if not _selftest["passed"]:
            raise RuntimeError(f"Pipeline self-test FAILED on startup: {_selftest['error']}")
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
                    exc_info=True,
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
                self._register_kill_switches(baseline)
            except Exception as exc:
                self._log.warning(
                    "orchestrator.drift_detector_init_failed",
                    error=str(exc),
                    action="continuing_without_drift_detection",
                    exc_info=True,
                )

        # Build signal engines
        model_dir = self._cfg.storage.model_dir
        for tf in self._timeframes:
            detector = self._detectors.get(tf.value)
            tf_trainer: ModelTrainer | None = self._trainers.get(tf.value)
            if detector is None or tf_trainer is None:
                self._log.warning("orchestrator.engine_skip", timeframe=tf.value)
                continue
            trainer = tf_trainer

            try:
                direction_model = ModelTrainer.load_direction(model_dir, self._symbol, tf.value)
                meta_model = ModelTrainer.load_meta(model_dir, self._symbol, tf.value)
            except FileNotFoundError:
                self._log.warning("orchestrator.models_not_found", timeframe=tf.value)
                continue

            # Ensemble is additive and optional -- a missing/failed load must not
            # block bringing up the direction/meta-driven engine (same fail-open
            # contract as the retrain path in _train_models()).
            try:
                ensemble = ModelTrainer.load_ensemble(model_dir, self._symbol, tf.value)
            except FileNotFoundError:
                ensemble = None
            except Exception as exc:
                self._log.warning(
                    "orchestrator.ensemble_load_failed",
                    timeframe=tf.value,
                    error=str(exc),
                    exc_info=True,
                )
                ensemble = None

            # TASK-008 online learner. One instance per (symbol, timeframe),
            # given its own directory so two timeframes never overwrite each
            # other's SGD state. Additive and optional on exactly the same
            # terms as the ensemble above: a load failure must not stop the
            # engine coming up, it only means starting from a cold model.
            try:
                online_trainer = OnlineTrainer(
                    model_dir=Path(model_dir)
                    / f"online_{self._symbol.replace('/', '_')}_{tf.value}"
                )
            except Exception as exc:
                self._log.warning(
                    "orchestrator.online_trainer_init_failed",
                    timeframe=tf.value,
                    error=str(exc),
                    exc_info=True,
                )
                online_trainer = None

            self._engines[tf.value] = SignalEngine(
                symbol=self._symbol,
                timeframe=tf,
                storage=self._storage,
                fetcher=self._fetcher,
                detector=detector,
                direction_model=direction_model,
                meta_model=meta_model,
                trainer=trainer,
                ensemble=ensemble,
                online_trainer=online_trainer,
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
        tasks.append(
            asyncio.create_task(self._allocation_rebalance_loop(), name="allocation_rebalance")
        )

        # Crypto-Box background data provider loops (no-op when CRYPTO_BOX!=true)
        if self._crypto_box.enabled:
            tasks.extend(self._crypto_box_provider_tasks())

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

    def _executor_for(self, tf: Timeframe) -> AnyExecutor | None:
        """
        Route a timeframe to its executor.

        Only primary_timeframe ever reaches self._executor when it's a
        LiveExecutor; every other timeframe is forced to paper regardless of
        the global trading_mode (spec: scalping/swing are paper-only).
        """
        if tf != self._primary_tf and self._non_primary_executor is not None:
            return self._non_primary_executor

        # A non-primary timeframe must never reach a LiveExecutor. Today
        # startup() always builds the paper executor when trading_mode=LIVE
        # and any non-primary timeframe is active, so this branch is
        # unreachable -- but the guard above was `is not None`, meaning any
        # future path that left it unset (a partially-completed startup, a
        # timeframe added after construction) would fall through to real
        # money rather than to nothing.
        #
        # Skipping the tick is the correct failure: a paper-only stream that
        # does not run costs a simulated trade, and the alternative costs a
        # real one the spec forbids.
        if tf != self._primary_tf and isinstance(self._executor, LiveExecutor):
            self._log.error(
                "orchestrator.non_primary_timeframe_has_no_paper_executor",
                timeframe=tf.value,
                reason="refusing to route a paper-only timeframe to the live executor",
            )
            return None

        return self._executor

    def _all_executors(self) -> list[AnyExecutor]:
        """Every distinct executor instance currently active, for lifecycle/monitoring."""
        executors: list[AnyExecutor] = []
        if self._executor is not None:
            executors.append(self._executor)
        if self._non_primary_executor is not None:
            executors.append(self._non_primary_executor)
        return executors

    def _crypto_box_provider_tasks(self) -> list[asyncio.Task[None]]:
        """Spawn background polling loops for all Crypto-Box data providers."""
        tasks: list[asyncio.Task[None]] = []
        try:
            from src.data.deribit_provider import DeribitProvider
            from src.data.exchange_flow_provider import ExchangeFlowProvider
            from src.data.macro_provider import MacroProvider
            from src.data.sentiment_provider import SentimentProvider

            sp = SentimentProvider()
            mp = MacroProvider()
            dp = DeribitProvider()
            xp = ExchangeFlowProvider()
            tasks.append(asyncio.create_task(sp.run_fg_loop(), name="cb_sentiment_fg"))
            tasks.append(asyncio.create_task(sp.run_rss_loop(), name="cb_sentiment_rss"))
            tasks.append(asyncio.create_task(mp.run_loop(), name="cb_macro"))
            tasks.append(asyncio.create_task(xp.run_loop(), name="cb_exchange_flows"))
            tasks.extend(
                asyncio.create_task(dp.run_loop(f"{coin}/USDT"), name=f"cb_deribit_{coin}")
                for coin in ("BTC", "ETH")
            )
        except Exception as exc:
            self._log.warning("orchestrator.cb_provider_tasks_failed", error=str(exc))
        return tasks

    async def shutdown(self) -> None:
        """Flush state, close all subsystems, and shut down training executor."""
        for executor in self._all_executors():
            await executor.shutdown()
        await get_monitor().stop()  # Patch B: clean monitor shutdown
        self._persist_online_trainers()
        # Shut down training thread pool cleanly — wait for any in-flight training job
        self._train_executor.shutdown(wait=True)
        self._log.info("orchestrator.shutdown_complete")

    def _persist_online_trainers(self) -> None:
        """
        Save each engine's online learner so its SGD state survives a restart.

        Best-effort per timeframe: the learner is a drift detector on top of
        the batch models, so losing one restart's worth of incremental state
        costs accuracy, never correctness — and it must not be able to abort
        the rest of shutdown, which still has executors to close.
        """
        for tf_value, engine in self._engines.items():
            trainer = getattr(engine, "_online_trainer", None)
            if trainer is None:
                continue
            try:
                trainer.save()
            except Exception as exc:
                self._log.warning(
                    "orchestrator.online_trainer_save_failed",
                    timeframe=tf_value,
                    error=str(exc),
                    exc_info=True,
                )

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
                    exc_info=True,
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

        executor = self._executor_for(tf)
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

        # Win/loss stats for Kelly. Non-primary timeframes always execute as
        # paper (see _executor_for) even when the global trading_mode is live,
        # so their trade history is stored under "paper" -- querying by the
        # global trading_mode here would find zero rows for them and silently
        # fall back to default win/loss stats every tick.
        effective_trading_mode = (
            TradingMode.PAPER.value
            if executor is self._non_primary_executor
            else self._cfg.trading_mode.value
        )
        recent_trades = await self._storage.fetch_trades(
            symbol=self._symbol,
            trading_mode=effective_trading_mode,
            limit=200,  # raised from 100 (VUL-020: larger window reduces overfit sizing)
        )
        # SCAN2-012: fetch_trades returns DESC order (most-recent first). Reverse to
        # chronological order before passing to compute_win_loss_stats so any
        # future time-order-sensitive analysis gets correct sequencing.
        pnl_history = [t.pnl_usd for t in reversed(recent_trades) if t.pnl_usd is not None]
        _, avg_win, avg_loss, _ = compute_win_loss_stats(pnl_history)

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
            # v3 unified ledger: publish this venue's book, then take the
            # correlation input from the ledger rather than from this one
            # executor. Each Orchestrator owns its own executor, so the
            # executor-only view could not see a position another symbol's
            # orchestrator held — the correlation ceiling was being computed
            # against a book strictly smaller than the real one, which biases
            # it toward "uncorrelated", i.e. toward sizing UP. The ledger is
            # process-wide, matching PortfolioCorrelationTracker's own scope.
            other_open_symbols = self._sync_and_read_ledger(
                self._venue_for(executor), open_positions
            )
            correlation_scalar = tracker.correlation_scalar(
                new_symbol=self._symbol,
                open_symbols=other_open_symbols,
            )

            # Strategy-level correlation is an independent ceiling on the
            # same position: two strategies can be uncorrelated as assets
            # yet run the same underlying bet. Both scalars multiply, and
            # Kelly stays the outer ceiling either way.
            #
            # Guarded separately from the asset scalar above rather than
            # sharing its except: the outer handler resets to 1.0, so a
            # fault here would discard an already-computed asset reduction
            # and size the position larger than the asset tracker asked
            # for. Failing to apply a ceiling must never remove one.
            try:
                correlation_scalar = combined_correlation_scalar(
                    asset_scalar=correlation_scalar,
                    strategy_scalar=self._strategy_correlation_scalar(open_positions),
                )
            except Exception as strategy_exc:
                self._log.error(
                    "orchestrator.strategy_correlation_scalar_failed",
                    error=str(strategy_exc),
                    retained_asset_scalar=round(correlation_scalar, 4),
                    exc_info=True,
                )
        except Exception as exc:
            self._log.error(
                "orchestrator.correlation_scalar_failed",
                error=str(exc),
                fallback_scalar=1.0,
                exc_info=True,
            )
            correlation_scalar = 1.0

        # v7 macro overlay: shrink-only, computed independently of the
        # correlation scalars above so a fault in either path cannot discard
        # the other's ceiling. Fails to None (no overlay) rather than to a
        # neutral scalar -- see _macro_exposure_budget.
        macro_budget: MacroExposureBudget | None = None
        try:
            macro_budget = await self._macro_exposure_budget(tf)
        except Exception as exc:
            self._log.error(
                "orchestrator.macro_exposure_budget_failed",
                error=str(exc),
                exc_info=True,
            )

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
            macro_budget=macro_budget,
            # GAP-003: the drift detector is built during startup AFTER the
            # engines exist, so it is supplied per tick rather than injected
            # at construction -- an engine built before it existed would
            # otherwise hold None for the life of the process.
            drift_detector=self._drift_detector,
        )

        # Crypto-Box augmentation — blends 18-engine ensemble into Kelly sizing.
        # Only active when CRYPTO_BOX=true; fails open to unmodified result.
        if self._crypto_box.enabled and result.kelly_result is not None:
            try:
                _tf_secs = TIMEFRAME_SECONDS.get(tf, 3600)
                _since_ms = int((time.time() - 300 * _tf_secs) * 1000)
                _bar_recs = await self._storage.fetch_bars(
                    self._symbol, tf.value, _since_ms, limit=300
                )
                bars = (
                    pd.DataFrame(
                        [
                            {
                                "timestamp_utc": b.ts,
                                "open": b.open,
                                "high": b.high,
                                "low": b.low,
                                "close": b.close,
                                "volume": b.volume,
                            }
                            for b in _bar_recs
                        ]
                    )
                    if _bar_recs
                    else None
                )
                spot = float(bars["close"].iloc[-1]) if bars is not None and len(bars) else 0.0
                _cache_snap = get_provider_cache().snapshot(self._symbol)
                _cb_data: dict[str, Any] = {"ohlcv": bars, "spot": spot, **_cache_snap}
                cb_signal = await self._crypto_box.get_signal(self._symbol, _cb_data)
                if cb_signal is not None:
                    # Circuit breaker: always suppress even when kelly_multiplier==0
                    if "manipulation_circuit_breaker" in cb_signal.warnings:
                        result = _dc_replace(
                            result,
                            tradeable=False,
                            skip_reason="crypto_box_circuit_breaker",
                        )
                    elif cb_signal.kelly_multiplier > 0.0:
                        # Scale Kelly by crypto-box confidence; halve on direction conflict
                        direction_match = (
                            cb_signal.direction == 0 or cb_signal.direction == result.direction
                        )
                        scale = cb_signal.kelly_multiplier if direction_match else 0.5
                        assert result.kelly_result is not None
                        new_adj = max(
                            0.0,
                            min(
                                result.kelly_result.adjusted_fraction * scale,
                                result.kelly_result.adjusted_fraction,
                            ),
                        )
                        new_kelly = _dc_replace(result.kelly_result, adjusted_fraction=new_adj)
                        result = _dc_replace(result, kelly_result=new_kelly)
                    try:
                        from src.diagnostics.audit_trail import get_audit_trail

                        get_audit_trail().record(
                            event_type="crypto_box_signal",
                            reason_code=cb_signal.regime,
                            details={
                                "symbol": self._symbol,
                                "direction": cb_signal.direction,
                                "confidence": cb_signal.confidence,
                                "kelly_multiplier": cb_signal.kelly_multiplier,
                                "warnings": ",".join(cb_signal.warnings),
                            },
                        )
                    except Exception:
                        pass
            except Exception as _cb_exc:
                self._log.warning("orchestrator.crypto_box_augment_failed", error=str(_cb_exc))

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
                changepoint_probability=result.changepoint_probability,
                agreement_score=result.regime_agreement_scalar,
            )
            await self._storage.upsert_regime_snapshot(snap)

        # Feed the registry adapter this tick's SignalResult. SignalEngine is
        # async and stateful, so SignalEngineStrategy cannot call it — it
        # translates a result handed to it. Without this the incumbent
        # strategy's generate_signal() would answer Signal(0, 0, 0) forever,
        # making it look permanently flat to anything reading the registry.
        self._publish_signal_to_registry(result)

        # TASK-007: push metrics snapshot to Prometheus gauges/counters
        try:
            _executor = getattr(self, "_executor", None)
            update_metrics(
                {
                    "signal_score": float(result.p_long - (1.0 - result.p_long)),
                    "regime_state": result.regime.state if result.regime else 0,
                    "prob_ranging": result.regime.prob_ranging if result.regime else 0.0,
                    "prob_trending": result.regime.prob_trending if result.regime else 0.0,
                    "prob_volatile": result.regime.prob_volatile if result.regime else 0.0,
                    "kelly_fraction": result.kelly_result.adjusted_fraction
                    if result.kelly_result
                    else 0.0,
                    "equity_usd": _executor.equity_usd if _executor else 0.0,
                    "open_positions": len(_executor.open_positions) if _executor else 0,
                }
            )
        except Exception as exc:
            # Metric failure must never affect the trade path -- but a silent
            # `pass` here would hide a real bug (e.g. a typo'd attribute)
            # indefinitely, since Prometheus scraping gives no feedback loop
            # back into this process. Log at warning so it's visible without
            # ever raising into the caller.
            self._log.warning("orchestrator.metrics_update_failed", error=str(exc), exc_info=True)

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
                # Drift-triggered retrain: immediately kick off model refresh
                # so the next window of signals uses an updated model.
                # Guard against overlap with a running scheduled retrain.
                prior = self._retrain_tasks.get(tf.value)
                if prior is None or prior.done():
                    self._log.info(
                        "orchestrator.drift_triggered_retrain",
                        timeframe=tf.value,
                    )
                    task = asyncio.create_task(
                        self._train_models(tf),
                        name=f"retrain_{tf.value}_drift",
                    )

                    def _drift_retrain_done(t: asyncio.Task, _tf: str = tf.value) -> None:
                        if not t.cancelled() and t.exception() is not None:
                            self._last_retrain_error[_tf] = str(t.exception())
                            self._log.error(
                                "orchestrator.drift_retrain_failed",
                                timeframe=_tf,
                                error=str(t.exception()),
                                exc_info=True,
                            )
                        if self._retrain_tasks.get(_tf) is t:
                            del self._retrain_tasks[_tf]

                    task.add_done_callback(_drift_retrain_done)
                    self._retrain_tasks[tf.value] = task
                return  # Skip this signal due to drift

            current_price = result.kelly_result.entry_price
            if current_price <= 0.0:
                # Fetch current price for fill simulation
                try:
                    current_price = await self._fetcher.fetch_ticker_price(self._symbol)
                except Exception as exc:
                    # VUL-ORCHESTRATOR-001: previous fallback used capital_usd which is
                    # equity in USD, not an asset price — would produce nonsensical position
                    # sizes. Skip the signal instead; a bad price is worse than no trade.
                    self._log.error(
                        "orchestrator.signal_skip_no_price",
                        timeframe=tf.value,
                        reason="ticker_fetch_failed_and_no_known_price",
                        error=str(exc),
                        exc_info=True,
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

            self._log.info(
                "orchestrator.signal_submitted",
                timeframe=tf.value,
                direction=result.direction,
                outcome=outcome,
                trade_id=trade_id,
            )

            # UI-001: a tradeable signal that didn't open a position (gate
            # rejection, approval denial/timeout) is a "missed trade" —
            # log it best-effort so the dashboard can surface it. Never
            # let a logging failure affect the trade path.
            if outcome != "opened":
                try:
                    await self._storage.insert_missed_trade(
                        MissedTradeRecord(
                            id=str(uuid.uuid4()),
                            symbol=self._symbol,
                            timeframe=tf.value,
                            direction=result.direction,
                            reason=outcome,
                            kelly_fraction=result.kelly_result.adjusted_fraction,
                            meta_label_prob=result.p_bet,
                            raw_signal=result.p_long,
                            regime_at_entry=result.regime.state if result.regime else 0,
                            notional_usd=result.kelly_result.notional_usd,
                            ts=int(time.time() * 1000),
                        )
                    )
                except Exception as exc:
                    self._log.warning(
                        "orchestrator.missed_trade_log_failed", error=str(exc), exc_info=True
                    )

        # Bar retention. StorageSettings.bar_cache_days configured a retention
        # window and prune_old_bars() implemented it on both backends, but
        # nothing ever called it -- bars accumulated for the life of the
        # deployment. Runs on the same primary-timeframe cadence as retraining
        # because it is the same kind of periodic housekeeping, and prunes
        # every active timeframe rather than only the primary one.
        if tf == self._primary_tf and self._tick_counts[tf.value] % _RETRAIN_INTERVAL_TICKS == 0:
            await self._prune_old_bars()

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

    async def _route_retrained_bundle(
        self,
        *,
        tf: Timeframe,
        version: str,
        direction_model: Any,
        meta_model: Any,
        detector: Any,
        ensemble: Any,
        metrics_records: tuple[ModelMetricsRecord, ...],
        live_gate_pass: bool,
    ) -> None:
        """
        Decides what a freshly trained bundle is allowed to do.

        Clearing the live gate is an *absolute* test (OOS Sharpe, max drawdown,
        trade count) — it says the candidate is good enough to trade, not that
        it is better than the model already trading. Retraining used to swap on
        the strength of that absolute test alone, so a worse model replaced a
        better one on every scheduled cycle. Shadow mode makes the candidate
        out-predict the incumbent on live bars first (v4 model registry).

        Three routes:
          - fails the live gate  -> discarded; the incumbent already passed it
          - shadow mode disabled -> swapped immediately (previous behaviour)
          - otherwise            -> shadowed until it earns the live slot
        """
        engine = self._engines[tf.value]

        if not live_gate_pass:
            # Not swapped *and* not recorded: the live gate reads the latest
            # metrics row, so writing this one would replace a passing
            # incumbent's record with a failing candidate's and halt live
            # trading on the strength of a model that never went live.
            self._log.warning(
                "orchestrator.retrain_discarded_live_gate_failed",
                timeframe=tf.value,
                version=version,
            )
            return

        if not self._cfg.xgboost.shadow_mode_enabled:
            for record in metrics_records:
                await self._storage.insert_model_metrics(record)
            await engine.swap_models(
                direction_model, meta_model, detector, ensemble=ensemble, model_id=version
            )
            return

        await engine.set_shadow_bundle(
            ShadowBundle(
                model_id=version,
                direction_model=direction_model,
                meta_model=meta_model,
                detector=detector,
                ensemble=ensemble,
                metrics=metrics_records,
            )
        )
        self._log.info(
            "orchestrator.retrain_entered_shadow",
            timeframe=tf.value,
            version=version,
            min_evaluations=self._cfg.xgboost.shadow_min_evaluations,
        )

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
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400,
        }.get(tf.value, 60)
        cutoff_ts = int(
            (datetime.now(tz=UTC).timestamp() - _HISTORY_BARS_FOR_TRAIN * tf_seconds) * 1000
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
            self._log.error("orchestrator.feature_build_failed", error=str(exc), exc_info=True)
            return

        await self._attach_intelligence_features(fm, tf)

        # HMM training — CPU bound
        detector = RegimeDetector(self._symbol, tf.value)
        try:
            await loop.run_in_executor(self._train_executor, detector.fit, fm.features)
            detector.save(self._cfg.storage.model_dir)
            self._detectors[tf.value] = detector
        except Exception as exc:
            self._log.error("orchestrator.hmm_train_failed", error=str(exc), exc_info=True)

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

            # Diversified prediction ensemble (ARIMA/XGBoost/LSTM/GP/TreeEnsemble) --
            # additive to direction/meta training, never blocks it. A failure here
            # (e.g. an optional dependency missing) must not prevent the direction/
            # meta models -- which just trained and saved successfully above -- from
            # being hot-swapped in below.
            ensemble = None
            try:
                ensemble = await loop.run_in_executor(
                    self._train_executor, trainer.train_ensemble, fm
                )
                await loop.run_in_executor(
                    self._train_executor,
                    trainer.save_ensemble,
                    ensemble,
                    self._cfg.storage.model_dir,
                )
            except Exception as exc:
                self._log.error("orchestrator.ensemble_train_failed", error=str(exc), exc_info=True)
                ensemble = None

            metrics_records = (
                dir_result.to_metrics_record("direction", tf.value, version),
                meta_result.to_metrics_record("meta_label", tf.value, version),
            )

            if tf.value in self._engines:
                new_dir = ModelTrainer.load_direction(
                    self._cfg.storage.model_dir, self._symbol, tf.value
                )
                new_meta = ModelTrainer.load_meta(
                    self._cfg.storage.model_dir, self._symbol, tf.value
                )
                await self._route_retrained_bundle(
                    tf=tf,
                    version=version,
                    direction_model=new_dir,
                    meta_model=new_meta,
                    detector=detector,
                    ensemble=ensemble,
                    metrics_records=metrics_records,
                    live_gate_pass=dir_result.live_gate_pass and meta_result.live_gate_pass,
                )
            else:
                # No engine on this timeframe, so nothing can be shadowed or
                # swapped — the metrics are still the OOS record of a training
                # run that happened and are persisted unconditionally.
                for record in metrics_records:
                    await self._storage.insert_model_metrics(record)

            self._log.info(
                "orchestrator.training_complete",
                timeframe=tf.value,
                oos_sharpe_dir=dir_result.oos_sharpe,
                oos_sharpe_meta=meta_result.oos_sharpe,
                dir_live_gate=dir_result.live_gate_pass,
                meta_live_gate=meta_result.live_gate_pass,
            )
        except Exception as exc:
            self._log.error("orchestrator.xgb_train_failed", error=str(exc), exc_info=True)

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
                for executor in self._all_executors():
                    # NEW-005: atomic reset via executor method — avoids torn read
                    # during concurrent mark_to_market.
                    equity = await executor.reset_daily_equity()
                    self._log.info(
                        "orchestrator.daily_reset",
                        equity_usd=equity,
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error("orchestrator.midnight_reset_error", error=str(exc), exc_info=True)
                await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # v9 -- rate-limited capital rebalancing
    # ------------------------------------------------------------------

    async def _allocation_rebalance_loop(self) -> None:
        """
        Advance the book's capital allocation one rate-limited step toward
        the performance-weighted target, on a fixed cadence.

        The allocator itself (performance_weighted_allocate) is stateless: it
        answers "given attribution as of right now, what split is optimal?".
        Applied directly, that makes the allocator a source of instability —
        a single unlucky window flips the split, and the next window flips it
        back, churning capital on noise. The controller keeps the incumbent
        allocation and moves at most max_allocation_shift_per_step toward the
        target per rebalance (Domain Prior: the same "no runaway automation"
        discipline that makes Kelly a ceiling rather than a target).

        The cadence lives here rather than in the API layer so allocation
        advances at a rate the operator configured, not at whatever rate a
        dashboard happens to poll /strategies/allocation.
        """
        portfolio = self._cfg.strategy_portfolio
        controller = get_allocation_controller(portfolio.max_allocation_shift_per_step)
        while self._running:
            try:
                await asyncio.sleep(portfolio.allocation_rebalance_interval_s)
                strategies = tuple(get_default_registry().all())
                if not strategies:
                    continue
                enabled_ids = get_strategy_kill_switch_manager().enabled_ids(
                    s.strategy_id for s in strategies
                )
                target = performance_weighted_allocate(strategies, enabled_ids)
                applied = controller.step_toward(target.fractions)
                self._log.info(
                    "orchestrator.allocation_rebalance",
                    method=target.method,
                    target={sid: round(w, 4) for sid, w in target.fractions.items()},
                    applied={sid: round(w, 4) for sid, w in applied.items()},
                    max_shift_per_step=controller.max_shift_per_step,
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error(
                    "orchestrator.allocation_rebalance_error", error=str(exc), exc_info=True
                )
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
                executors = self._all_executors()
                if not executors:
                    continue

                # Skip the price fetch entirely when every executor is flat --
                # preserves the original no-op-when-flat behavior even with a
                # second (non-primary) executor in the mix.
                any_open = False
                for executor in executors:
                    if await executor.open_positions_safe():
                        any_open = True
                        break
                if not any_open:
                    continue

                try:
                    price = await self._fetcher.fetch_ticker_price(self._symbol)
                except Exception as exc:
                    self._log.warning(
                        "orchestrator.position_monitor_price_fetch_failed",
                        error=str(exc),
                        exc_info=True,
                    )
                    continue
                if price <= 0.0:
                    continue

                for executor in executors:
                    await self._monitor_positions_for(executor, price)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._log.error(
                    "orchestrator.position_monitor_loop_error", error=str(exc), exc_info=True
                )
                await asyncio.sleep(5)

    def _register_kill_switches(self, baseline: PerformanceBaseline) -> None:
        """
        Give every registered strategy a kill switch against ``baseline``.

        The kill switch auto-disables a strategy whose live performance
        drifts below its promotion-time baseline. It was previously
        unreachable — nothing in src/ called register_strategy(), so
        StrategyKillSwitchManager held no state and every strategy ran
        unguarded.

        The baseline is the same one the global drift detector just took
        from the trained primary model, so this only runs where a *real*
        baseline exists. Inventing a default here would be worse than no
        kill switch: a fabricated Sharpe produces fabricated drift, and a
        strategy would be disabled on evidence that was never measured.

        Re-registration raises inside the manager (a kill switch must not
        silently reset its accumulated evidence), so already-registered
        strategies are skipped — startup can run twice in a process.
        """
        manager = get_strategy_kill_switch_manager()
        for strategy in get_default_registry().all():
            try:
                if manager.is_registered(strategy.strategy_id):
                    continue
                manager.register_strategy(strategy.strategy_id, baseline)
            except Exception as exc:
                self._log.warning(
                    "orchestrator.kill_switch_registration_failed",
                    strategy_id=strategy.strategy_id,
                    error=str(exc),
                    exc_info=True,
                )

    async def _macro_exposure_budget(self, tf: Timeframe) -> MacroExposureBudget | None:
        """
        v7 portfolio-level macro overlay for this tick.

        Reads the recent intelligence-feature window for this symbol/timeframe,
        classifies aggregate risk appetite, and converts it into a shrink-only
        exposure scalar. Returns None -- meaning "apply no overlay" -- when the
        overlay is disabled or the window carries no usable macro data.

        None, not a neutral MacroIndicators: a neutral reading still maps to a
        ~0.62 scalar, so defaulting to neutral would quietly cut every position
        by a third whenever the intelligence table happens to be empty.
        """
        # self._cfg.risk directly, not effective_risk_settings: the macro
        # overlay is not a self-tunable parameter, so overlaying the registry
        # here would only imply an authority it does not have.
        risk = self._cfg.risk
        if not risk.macro_exposure_enabled:
            return None

        lookback = risk.macro_exposure_lookback_bars
        latest_ts = await self._storage.latest_bar_ts(self._symbol, tf.value)
        if latest_ts is None:
            return None
        # since_ts, not LIMIT: fetch_intelligence_features orders ascending, so
        # a bare limit would return the OLDEST rows, not the most recent ones.
        since_ts = latest_ts - lookback * TIMEFRAME_SECONDS[tf] * 1000

        features = await self._storage.fetch_intelligence_features(
            symbol=self._symbol,
            timeframe=tf.value,
            since_ts=since_ts,
        )
        indicators = build_macro_indicators(features)
        if indicators is None:
            return None
        return compute_macro_exposure_scalar(classify_macro_regime(indicators))

    async def _prune_old_bars(self) -> None:
        """
        Drop bars older than StorageSettings.bar_cache_days.

        Best-effort per timeframe: a pruning failure is a housekeeping
        problem, not a trading one, and must never take down a tick loop that
        is otherwise healthy. Each timeframe is pruned independently so one
        failure does not skip the rest.

        Retention is deliberately checked against the configured window every
        cycle rather than tracked incrementally -- prune_old_bars() is
        idempotent, so a missed cycle self-heals on the next one instead of
        leaving a permanent gap in what gets collected.
        """
        keep_days = int(self._cfg.storage.bar_cache_days)
        for tf in self._timeframes:
            try:
                deleted = await self._storage.prune_old_bars(self._symbol, tf.value, keep_days)
            except Exception as exc:
                self._log.warning(
                    "orchestrator.prune_bars_failed",
                    timeframe=tf.value,
                    error=str(exc),
                    exc_info=True,
                )
                continue
            if deleted:
                self._log.info(
                    "orchestrator.pruned_bars",
                    timeframe=tf.value,
                    deleted=deleted,
                    keep_days=keep_days,
                )

    async def _attach_intelligence_features(self, fm: object, tf: Timeframe) -> None:
        """
        Join the stored intelligence history onto the training matrix (GAP-015).

        The trainer resolves its column set with
        ``get_active_feature_columns(coverage=getattr(fm, "intelligence_coverage", None))``.
        Nothing ever set that attribute, so coverage was always None, the
        active set was always the 8 base columns, and the 18 intelligence
        features never reached training — while inference happily injected
        them into the vector, where predict_direction/predict_meta sliced them
        straight back off to match the model's ``n_features_in_``. Computed,
        injected, and silently discarded.

        Both halves are needed. Attaching coverage without joining the columns
        would only make the trainer log "column in active set but absent from
        FeatureMatrix" and drop them again; joining without coverage would
        leave the active set at the base 8 and ignore the joined columns.

        Left-join on bar timestamp: bars are authoritative, and a bar with no
        intelligence row keeps its base features rather than being dropped
        from training. The resulting NaNs are what the coverage gate is for.

        Best-effort. Intelligence is an enrichment; failing to attach it must
        degrade training to base features, never abort the retrain.
        """
        try:
            coverage_report = await self._storage.intelligence_feature_coverage(
                self._symbol, tf.value
            )
            coverage = coverage_report.get("coverage") if coverage_report else None
            if not coverage:
                return

            intel = await self._storage.fetch_intelligence_features(
                symbol=self._symbol, timeframe=tf.value
            )
            if intel is None or intel.empty:
                return

            features = getattr(fm, "features", None)
            if features is None or features.empty:
                return

            joined = features.join(intel, how="left")
            fm.features = joined  # type: ignore[attr-defined]
            fm.intelligence_coverage = coverage  # type: ignore[attr-defined]
            self._log.info(
                "orchestrator.intelligence_attached",
                timeframe=tf.value,
                intelligence_columns=len(intel.columns),
                rows_with_intelligence=int(intel.index.isin(features.index).sum()),
            )
        except Exception as exc:
            self._log.warning(
                "orchestrator.intelligence_attach_failed",
                timeframe=tf.value,
                error=str(exc),
                exc_info=True,
            )

    def _venue_for(self, executor: AnyExecutor) -> str:
        """
        Ledger venue key for *executor*.

        Paper fills are not exchange exposure and must not be netted against
        live exposure on the same symbol, so they get their own venue. That
        matters in live mode, where the non-primary timeframes run on a
        separate paper executor alongside the real one.
        """
        return EXCHANGE_BINANCE if isinstance(executor, LiveExecutor) else "paper"

    def _sync_and_read_ledger(
        self, venue: str, open_positions: list[dict[str, object]]
    ) -> list[str]:
        """
        Republish *venue*'s slice of the unified ledger, then return every
        OTHER symbol currently carrying exposure anywhere in the book.

        Self is excluded because correlating a symbol with itself always
        reads 1.0 and would say nothing about diversification.

        One executor owns exactly one venue, so the venue's rows are replaced
        wholesale rather than merged — a position closed since the last tick
        has to disappear, and an incremental update would leave it behind and
        overstate exposure forever.

        Falls back to the executor-only symbol list on any ledger fault: a
        smaller book biases the correlation ceiling toward 1.0, so this path
        must not also be able to raise and lose the ceiling entirely.
        """
        fallback = [
            cast("str", p["symbol"]) for p in open_positions if p.get("symbol") != self._symbol
        ]
        try:
            ledger = get_unified_ledger()
            current = _aggregate_venue_positions(venue, open_positions)
            live_symbols = {p.symbol for p in current}
            for stale in ledger.all_positions:
                if stale.venue == venue and stale.symbol not in live_symbols:
                    ledger.clear_position(venue, stale.symbol)
            for position in current:
                ledger.record_position(position)

            # gross, not net: a long on one venue and a short of the same size
            # on another still means the book is exposed to that symbol's
            # correlation structure, even though the net quantity is zero.
            return sorted(
                {
                    p.symbol
                    for p in ledger.all_positions
                    if p.symbol != self._symbol and ledger.gross_exposure(p.symbol) > 0.0
                }
            )
        except Exception as exc:
            self._log.error(
                "orchestrator.unified_ledger_sync_failed",
                venue=venue,
                error=str(exc),
                exc_info=True,
            )
            return fallback

    def _strategy_correlation_scalar(self, open_positions: list[dict[str, object]]) -> float:
        """
        Sizing scalar for the incumbent strategy against the other strategies
        currently holding capital.

        Returns 1.0 (no reduction) when no other strategy holds a position,
        which is the normal case while signal_engine_v1 is the only enabled
        strategy — the ceiling only bites once the portfolio is genuinely
        multi-strategy.
        """
        other_active = sorted(
            {
                str(p.get("strategy_id", ""))
                for p in open_positions
                if p.get("strategy_id") and p.get("strategy_id") != STRATEGY_ID_SIGNAL_ENGINE
            }
        )
        if not other_active:
            return 1.0
        return get_strategy_correlation().correlation_scalar(
            new_strategy_id=STRATEGY_ID_SIGNAL_ENGINE,
            active_strategy_ids=other_active,
        )

    def _push_strategy_returns(self, positions: list[dict[str, object]]) -> None:
        """
        Push one mark-to-market interval's realized return per strategy.

        StrategyCorrelationTracker answers "is this strategy's return stream
        correlated with the others currently holding capital?", but it had
        no producer — nothing in src/ ever called push_strategy_returns, so
        the tracker was empty and every scalar it produced was a no-op 1.0.

        The return for an interval is the change in unrealized P&L over that
        strategy's open notional, aggregated across its positions. Using the
        mark-to-market delta rather than closed-trade P&L matters for
        correctness here: correlation is only meaningful across *aligned*
        series, and closed trades arrive at whatever irregular times each
        strategy happens to exit. Every strategy is marked on the same tick.

        A strategy's first appearance produces no return — there is no prior
        mark to difference against — and positions that closed since the last
        call are dropped so their stale marks cannot leak into a later delta.
        """
        pnl_by_strategy: dict[str, float] = {}
        notional_by_strategy: dict[str, float] = {}
        current_marks: dict[str, float] = {}

        for pos in positions:
            strategy_id = str(pos.get("strategy_id", ""))
            trade_id = str(pos.get("trade_id", ""))
            if not strategy_id or not trade_id:
                continue
            unrealized = float(cast("float", pos.get("unrealized_pnl", 0.0)))
            notional = float(cast("float", pos.get("notional_usd", 0.0)))
            current_marks[trade_id] = unrealized
            prior = self._last_unrealized_by_trade.get(trade_id)
            if prior is None or notional <= 0.0:
                continue
            pnl_by_strategy[strategy_id] = pnl_by_strategy.get(strategy_id, 0.0) + (
                unrealized - prior
            )
            notional_by_strategy[strategy_id] = (
                notional_by_strategy.get(strategy_id, 0.0) + notional
            )

        # Replace wholesale: trade_ids absent from this snapshot are closed,
        # and keeping their last mark would let a stale value resurface as a
        # spurious delta if the id were ever reused.
        self._last_unrealized_by_trade = current_marks

        returns = {
            sid: pnl_by_strategy[sid] / notional_by_strategy[sid]
            for sid in pnl_by_strategy
            if notional_by_strategy.get(sid, 0.0) > 0.0
        }
        if not returns:
            return
        try:
            get_strategy_correlation().push_strategy_returns(returns)
        except Exception as exc:
            self._log.warning(
                "orchestrator.strategy_returns_push_failed",
                error=str(exc),
                exc_info=True,
            )

    def _record_kill_switch_outcome(
        self,
        *,
        strategy_id: str,
        pnl_usd: float,
        actual_direction: int,
        current_equity: float,
        now_ms: int,
    ) -> None:
        """
        Feed one closed trade to that strategy's kill switch, then evaluate.

        Recording and evaluating are paired deliberately: drift is only
        checked on new evidence, so a strategy that has stopped trading
        cannot be disabled by a stale window, and one that is trading badly
        is re-evaluated on every close rather than on a timer.

        Skipped when the strategy has no kill switch — that means no
        baseline was ever measured for it (see _register_kill_switches),
        and there is nothing to compare against.

        predicted_prob is 0.5 for the same reason the global drift feed
        uses it: the confidence that produced the entry is not carried on
        the position record. PnL, direction and equity are still exact.

        Never raises: a kill-switch fault must not turn a completed exit
        into an error path.
        """
        if not strategy_id:
            return
        try:
            manager = get_strategy_kill_switch_manager()
            if not manager.is_registered(strategy_id):
                return
            manager.record_trade_outcome(
                strategy_id=strategy_id,
                pnl_usd=pnl_usd,
                predicted_prob=0.5,
                actual_direction=actual_direction,
                current_equity=current_equity,
                starting_equity=self._cfg.starting_capital_usd,
            )
            drift = manager.evaluate(strategy_id, now_ms=now_ms)
            if drift.drifted:
                self._log.warning(
                    "orchestrator.strategy_kill_switched",
                    strategy_id=strategy_id,
                    reason=drift.reason,
                    metric=drift.metric,
                )
        except Exception as exc:
            self._log.warning(
                "orchestrator.kill_switch_record_failed",
                strategy_id=strategy_id,
                error=str(exc),
                exc_info=True,
            )

    def _publish_signal_to_registry(self, result: SignalResult) -> None:
        """
        Hand this tick's SignalResult to the registered signal-engine adapter.

        No-op when signal_engine_v1 is not registered (STRATEGY_SIGNAL_ENGINE_
        ENABLED=false) or when the registered object is some other
        implementation — the registry is keyed by strategy_id, not type, so
        the isinstance check is what makes submit_result() safe to call.

        Never raises into the trade path: this is observability plumbing for
        capital allocation and attribution, and a failure here must not stop
        an otherwise valid signal from reaching the executor.
        """
        try:
            strategy = get_default_registry().get(STRATEGY_ID_SIGNAL_ENGINE)
            if isinstance(strategy, SignalEngineStrategy):
                strategy.submit_result(result)
        except Exception as exc:
            self._log.warning(
                "orchestrator.registry_signal_publish_failed", error=str(exc), exc_info=True
            )

    async def _monitor_positions_for(self, executor: AnyExecutor, price: float) -> None:
        """Mark-to-market and stop-loss/take-profit/time-exit for one executor's positions."""
        await executor.mark_to_market({self._symbol: price})

        controls = await runtime_config.get_risk_controls()
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)

        # Re-snapshot after marking so unrealized_pnl_pct reflects the
        # price just fetched above, not a stale value from the last tick.
        positions = await executor.open_positions_safe()
        self._push_strategy_returns(positions)
        for pos in positions:
            if pos.get("symbol") != self._symbol:
                continue
            exit_reason = check_position_exit(
                unrealized_pnl_pct=cast("float", pos["unrealized_pnl_pct"]),
                entry_ts_ms=cast("int", pos["entry_ts"]),
                now_ts_ms=now_ms,
                stop_loss_enabled=cast("bool", controls["stop_loss_enabled"]),
                stop_loss_pct=cast("float", controls["stop_loss_pct"]),
                take_profit_enabled=cast("bool", controls["take_profit_enabled"]),
                take_profit_pct=cast("float", controls["take_profit_pct"]),
                max_holding_period_s=cast("float", controls["max_holding_period_s"]),
                trailing_stop_enabled=cast("bool", controls.get("trailing_stop_enabled", False)),
                trailing_stop_pct=cast("float", controls.get("trailing_stop_pct", 1.5)),
                peak_unrealized_pct=cast("float", pos.get("peak_unrealized_pct", 0.0)),
            )
            if exit_reason is None:
                continue
            try:
                net_pnl = await executor.close_position(
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

                # Record trade outcome for drift detection (GAP-003).
                # predicted_prob is not tracked on the position record, so a
                # neutral 0.5 is used — drift detection still gets accurate
                # PnL/direction/equity signal, just without confidence-weighted
                # prediction tracking for auto-closed trades.
                if self._drift_detector:
                    try:
                        await self._drift_adapter.record_closed_trade(
                            trade_id=str(pos["trade_id"]),
                            exit_price=price,
                            pnl_usd=net_pnl,
                            predicted_prob=0.5,
                            actual_direction=(1 if pos["direction"] == "long" else -1),
                            current_equity=executor.equity_usd,
                            starting_equity=self._cfg.starting_capital_usd,
                        )
                    except Exception as exc:
                        self._log.warning(
                            "orchestrator.drift_record_failed",
                            trade_id=pos["trade_id"],
                            error=str(exc),
                            exc_info=True,
                        )

                # Same outcome, per strategy. The global drift detector above
                # sees the book as one series; the kill switch keeps a
                # separate detector per strategy_id so one decaying strategy
                # is disabled without halting the others.
                self._record_kill_switch_outcome(
                    strategy_id=str(pos.get("strategy_id", "")),
                    pnl_usd=net_pnl,
                    actual_direction=(1 if pos["direction"] == "long" else -1),
                    current_equity=executor.equity_usd,
                    now_ms=now_ms,
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
                    exc_info=True,
                )
