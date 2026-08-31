"""
Auto-tuning scheduler -- the "explicit startup step" referenced by
src/tuning/bootstrap.py's module docstring, and the automated counterpart
to the manual tuning entrypoint that the config purge (#144) removed;
this module reuses the exact same process-wide singletons from
src/tuning/state.py so both paths share one audit trail / version store).

Started once from the API lifespan when SelfTuningSettings.enabled is
true. Every safety rail from the original design stays in force:

  - SelfTuningSettings.enabled is still the master kill switch (off by
    default; operator must set SELF_TUNING_ENABLED=true).
  - SelfTuningSettings.shadow_mode stays True by default -- accepted
    challengers are logged as WOULD_PROMOTE, never written to
    VersionedConfigStore, until an operator sets SELF_TUNING_SHADOW_MODE=false.
  - tuning_pause_state (the operator's runtime pause switch, POST
    /tuning/pause) is honored -- pausing via the API stops this loop's
    attempts on the next cycle, same as it stops a manual script run.
  - TuningRunner still enforces the per-parameter cooldown + gate + never-
    regress checks on every attempt.

`hmm.entropy_threshold` / `hmm.entropy_scalar_floor` (Phase 4),
`risk.slippage_impact_coeff_bps` (Phase 8 item 2), the five
`features.*_window` parameters (Phase 8 item 3), the eight
`xgboost.*` hyperparameters (Phase 8 item 4), and `risk.ensemble_blend_weight`
each have a working backtest harness (run_entropy_threshold_backtest /
run_slippage_coeff_backtest / run_feature_window_backtest /
run_xgboost_hyperparam_backtest / run_ensemble_blend_backtest
respectively). Any other registered parameter with no evaluate_fn is
intentionally left unscheduled here.

`risk.ensemble_blend_weight` additionally requires closed trades where
EnsemblePredictor was actually blended in at signal time (nonzero blend
weight while a fitted predictor was injected into SignalEngine) -- on a
deployment where blending has never been enabled,
ensemble_blend_samples_from_trades() returns an empty list every cycle and
that attempt is skipped cleanly, same as the feature-window harness's
missing-model case.

The feature-window harness additionally requires a previously trained,
saved direction model (ModelTrainer.load_direction) -- on a fresh
deployment with no model trained yet, that parameter group is skipped
cleanly every cycle (FileNotFoundError is an expected state, not an
error) until training produces one.

The XGBoost hyperparameter harness is materially more expensive than the
other three (it fits real models via full CPCV retraining, not a cheap
vectorised replay) -- per the design doc, it (a) only runs every
`xgboost_cycle_interval`-th cycle, not every interval tick, and (b) runs
via `loop.run_in_executor` so a multi-second-to-minutes retrain does not
block this process's asyncio event loop (the live API/trading loop runs
on the same loop).
"""

from __future__ import annotations

import asyncio
import functools
import math
import time

import pandas as pd
import structlog
from xgboost import XGBClassifier

from src.config import Settings
from src.data.storage import AnyStorageBackend
from src.features.pipeline import build_feature_matrix
from src.models.trainer import ModelTrainer
from src.tuning.backtest_harness import (
    XGBOOST_INT_FIELDS,
    EnsembleBlendSample,
    SlippageFillSample,
    TradeSample,
    ensemble_blend_samples_from_trades,
    run_ensemble_blend_backtest,
    run_entropy_threshold_backtest,
    run_feature_window_backtest,
    run_slippage_coeff_backtest,
    run_xgboost_hyperparam_backtest,
)
from src.tuning.bootstrap import (
    FEATURE_WINDOW_FIELDS,
    XGBOOST_HYPERPARAM_FIELDS,
    register_ensemble_blend_weight,
    register_feature_window_param,
    register_garch_vol_threshold,
    register_hmm_entropy_scalar_floor,
    register_hmm_entropy_threshold,
    register_slippage_impact_coeff,
    register_xgboost_hyperparam_param,
)
from src.tuning.evaluator import MetricComparison
from src.tuning.proposer import Proposal
from src.tuning.redteam_scheduler import RedTeamScheduler
from src.tuning.registry import TunableParameter
from src.tuning.state import parameter_registry, pause_state, runner, version_store


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_MIN_SAMPLES = 30  # below this, CPCV folds would be too thin to mean anything
_MIN_FEATURE_BARS = 300  # below this, CPCV folds over bars would be too thin
_FEATURE_BAR_FETCH_LIMIT = 5000


def _shannon_entropy(p_ranging: float, p_trending: float, p_volatile: float) -> float:
    """Normalized Shannon entropy of the 3-state posterior -- same
    normalization as RegimeDetector (src/regime/detector.py) so historical
    entropy is comparable to the live value used by position_scalar()."""
    probs = [p for p in (p_ranging, p_trending, p_volatile) if p > 0.0]
    if not probs:
        return 0.0
    max_entropy = math.log(3)
    if max_entropy <= 0.0:
        return 0.0
    raw = -sum(p * math.log(p) for p in probs)
    return max(0.0, min(1.0, raw / max_entropy))


class AutoTuningScheduler:
    """Runs one propose/evaluate/gate attempt per registered hmm-entropy
    parameter on a fixed wall-clock interval. TuningRunner's own cooldown
    still applies on top of this interval."""

    def __init__(
        self,
        storage: AnyStorageBackend,
        settings: Settings,
        symbol: str,
        timeframe: str,
        interval_hours: float = 1.0,
        xgboost_cycle_interval: int = 24,
    ) -> None:
        self._storage = storage
        self._settings = settings
        self._symbol = symbol
        self._timeframe = timeframe
        self._interval_s = max(60.0, interval_hours * 3600.0)
        # XGBoost hyperparameter attempts do a full CPCV retrain and are far
        # more expensive than the other three parameter groups -- only run
        # them every Nth cycle, not every interval tick. A freshly
        # constructed scheduler starts at cycle 0 (0 % N == 0 for any N), so
        # the first cycle -- whether reached via _loop() or a direct
        # _attempt_all() call in tests -- always attempts them once.
        self._xgboost_cycle_interval = max(1, xgboost_cycle_interval)
        # E-09 walk-forward retrain: runs every N cycles (default 48h at 1h/cycle)
        self._e09_retrain_interval = 48
        self._cycle_count = 0
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        # v10 red-team cadence tracker (src/tuning/redteam_scheduler.py):
        # this scheduler only tracks *when* a full-system stress replay is
        # due -- it never runs one itself. Actually executing
        # stress_simulator.py against the live allocation requires
        # meta_allocator.py to be producing a real allocation first (still
        # unwired -- see DECISION_LOG.md), so for now this cycle only logs a
        # recurring reminder once the interval elapses; record_run() is left
        # for whatever caller eventually performs the real replay.
        self._redteam_scheduler = RedTeamScheduler()

    def start(self) -> None:
        if not parameter_registry.is_registered("hmm.entropy_threshold"):
            register_hmm_entropy_threshold(parameter_registry, self._settings, version_store)
        if not parameter_registry.is_registered("hmm.entropy_scalar_floor"):
            register_hmm_entropy_scalar_floor(parameter_registry, self._settings, version_store)
        if not parameter_registry.is_registered("risk.slippage_impact_coeff_bps"):
            register_slippage_impact_coeff(parameter_registry, self._settings, version_store)
        if not parameter_registry.is_registered("risk.ensemble_blend_weight"):
            register_ensemble_blend_weight(parameter_registry, self._settings, version_store)
        # Not auto-scheduled below: visible via /self-tuning/status but not
        # auto-cycled until a vol-targeting backtest harness exists (see
        # register_garch_vol_threshold docstring). ensemble_blend_weight used
        # to share this state; it now has run_ensemble_blend_backtest and is
        # scheduled with the rest.
        if not parameter_registry.is_registered("risk.garch_vol_threshold"):
            register_garch_vol_threshold(parameter_registry, self._settings, version_store)
        for field_name in FEATURE_WINDOW_FIELDS:
            if not parameter_registry.is_registered(f"features.{field_name}"):
                register_feature_window_param(
                    parameter_registry, field_name, self._settings, version_store
                )
        for field_name in XGBOOST_HYPERPARAM_FIELDS:
            if not parameter_registry.is_registered(f"xgboost.{field_name}"):
                register_xgboost_hyperparam_param(
                    parameter_registry, field_name, self._settings, version_store
                )
        self._task = asyncio.create_task(self._loop(), name="auto_tuning_scheduler")
        log.info(
            "tuning.scheduler_started",
            interval_hours=self._interval_s / 3600.0,
            shadow_mode=self._settings.self_tuning.shadow_mode,
        )

    def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                if await pause_state.is_paused():
                    log.info("tuning.scheduler_paused")
                else:
                    # Attempt BEFORE incrementing -- _attempt_all() reads
                    # self._cycle_count for the XGBoost throttle gate, and a
                    # freshly constructed scheduler starts at cycle 0 (0 % N
                    # == 0 for any N), so this must still be 0 on the first
                    # real invocation for that first-cycle attempt to fire,
                    # matching _attempt_all()'s own doc comment and the
                    # direct-_attempt_all() test path.
                    await self._attempt_all()
                    await self._maybe_retrain_e09()
                    self._cycle_count += 1
                    self._check_redteam_due()
            except Exception as exc:
                log.error("tuning.scheduler_attempt_failed", error=str(exc), exc_info=True)
            try:
                await asyncio.sleep(self._interval_s)
            except asyncio.CancelledError:
                return

    def _check_redteam_due(self) -> None:
        """
        Logs a recurring reminder once the v10 red-team cadence elapses.
        Deliberately does not call self._redteam_scheduler.record_run() --
        that would falsely mark a replay as having happened. Stays "due"
        every cycle until a real caller runs stress_simulator.py against
        the live allocation and records it.
        """
        now_ms = int(time.time() * 1000)
        if self._redteam_scheduler.is_due(now_ms):
            log.warning(
                "tuning.redteam_stress_replay_due",
                last_run_ms=(
                    self._redteam_scheduler.last_run.ran_at_ms
                    if self._redteam_scheduler.last_run
                    else None
                ),
            )

    async def _attempt_all(self) -> None:
        # NOTE: each parameter group below (entropy, slippage, feature-window)
        # draws on independent data (trades vs. bars) and must not be gated
        # behind another group's sample-sufficiency check -- an `if
        # insufficient: return` here previously skipped slippage AND
        # feature-window attempts too whenever entropy had too few closed
        # trades, even though feature-window tuning needs only bar history.
        # Closed-trade count for the trade half of the runner's cadence guard
        # (SelfTuningSettings.min_trades_between_attempts). Computed once per
        # cycle and shared across every attempt below, so all parameters
        # measure "new evidence since my last attempt" against the same
        # snapshot rather than drifting apart within one cycle.
        closed_trade_count = await self._closed_trade_count()

        samples = await self._build_trade_samples()
        if len(samples) < _MIN_SAMPLES:
            log.info("tuning.scheduler_insufficient_samples", n_samples=len(samples))
        else:
            for param_name in ("hmm.entropy_threshold", "hmm.entropy_scalar_floor"):

                def evaluate(
                    _param: TunableParameter,
                    proposal: Proposal,
                    _param_name: str = param_name,
                ) -> list[MetricComparison]:
                    # Read the registry's current champion value fresh on
                    # EVERY call, not once before the loop -- if the
                    # hmm.entropy_threshold iteration above already promoted
                    # a challenger (registry.update_current) within this
                    # same _attempt_all() cycle, the hmm.entropy_scalar_floor
                    # iteration's "held constant" companion value must
                    # reflect that promotion too, not a value captured
                    # before this cycle even started (see
                    # src/tuning/live_overrides.py).
                    champion_threshold = parameter_registry.get("hmm.entropy_threshold").current
                    champion_floor = parameter_registry.get("hmm.entropy_scalar_floor").current
                    if _param_name == "hmm.entropy_threshold":
                        return run_entropy_threshold_backtest(
                            samples,
                            champion_threshold=proposal.champion_value,
                            champion_floor=champion_floor,
                            challenger_threshold=proposal.challenger_value,
                            challenger_floor=champion_floor,
                            features_cfg=self._settings.features,
                        )
                    return run_entropy_threshold_backtest(
                        samples,
                        champion_threshold=champion_threshold,
                        champion_floor=proposal.champion_value,
                        challenger_threshold=champion_threshold,
                        challenger_floor=proposal.challenger_value,
                        features_cfg=self._settings.features,
                    )

                try:
                    result = runner.attempt(
                        param_name,
                        evaluate,
                        primary_metric="oos_sharpe",
                        closed_trade_count=closed_trade_count,
                    )
                    log.info(
                        "tuning.scheduler_attempt",
                        param=param_name,
                        attempted=result.attempted,
                        accepted=result.accepted,
                        promoted=result.promoted,
                        reasons=result.reasons,
                    )
                except Exception as exc:
                    log.error(
                        "tuning.scheduler_attempt_error",
                        param=param_name,
                        error=str(exc),
                        exc_info=True,
                    )

        slippage_samples = await self._build_slippage_samples()
        if len(slippage_samples) < _MIN_SAMPLES:
            log.info(
                "tuning.scheduler_insufficient_slippage_samples", n_samples=len(slippage_samples)
            )
        else:

            def evaluate_slippage(
                _param: TunableParameter, proposal: Proposal
            ) -> list[MetricComparison]:
                return run_slippage_coeff_backtest(
                    slippage_samples,
                    champion_coeff=proposal.champion_value,
                    challenger_coeff=proposal.challenger_value,
                    features_cfg=self._settings.features,
                )

            try:
                result = runner.attempt(
                    "risk.slippage_impact_coeff_bps",
                    evaluate_slippage,
                    primary_metric="slippage_prediction_accuracy",
                    closed_trade_count=closed_trade_count,
                )
                log.info(
                    "tuning.scheduler_attempt",
                    param="risk.slippage_impact_coeff_bps",
                    attempted=result.attempted,
                    accepted=result.accepted,
                    promoted=result.promoted,
                    reasons=result.reasons,
                )
            except Exception as exc:
                log.error(
                    "tuning.scheduler_attempt_error",
                    param="risk.slippage_impact_coeff_bps",
                    error=str(exc),
                    exc_info=True,
                )

        ensemble_samples = await self._build_ensemble_blend_samples()
        if len(ensemble_samples) < _MIN_SAMPLES:
            log.info(
                "tuning.scheduler_insufficient_ensemble_samples", n_samples=len(ensemble_samples)
            )
        else:

            def evaluate_ensemble_blend(
                _param: TunableParameter, proposal: Proposal
            ) -> list[MetricComparison]:
                return run_ensemble_blend_backtest(
                    ensemble_samples,
                    champion_weight=proposal.champion_value,
                    challenger_weight=proposal.challenger_value,
                    features_cfg=self._settings.features,
                )

            try:
                result = runner.attempt(
                    "risk.ensemble_blend_weight",
                    evaluate_ensemble_blend,
                    primary_metric="oos_sharpe",
                    closed_trade_count=closed_trade_count,
                )
                log.info(
                    "tuning.scheduler_attempt",
                    param="risk.ensemble_blend_weight",
                    attempted=result.attempted,
                    accepted=result.accepted,
                    promoted=result.promoted,
                    reasons=result.reasons,
                )
            except Exception as exc:
                log.error(
                    "tuning.scheduler_attempt_error",
                    param="risk.ensemble_blend_weight",
                    error=str(exc),
                )

        bars_df = await self._build_feature_bars_df()
        if bars_df is None or len(bars_df) < _MIN_FEATURE_BARS:
            log.info(
                "tuning.scheduler_insufficient_feature_bars",
                n_bars=0 if bars_df is None else len(bars_df),
            )
        else:
            direction_model = self._load_direction_model()
            if direction_model is None:
                log.info("tuning.scheduler_no_direction_model")
            else:
                for field_name in FEATURE_WINDOW_FIELDS:
                    param_name = f"features.{field_name}"

                    def evaluate_feature_window(
                        _param: TunableParameter,
                        proposal: Proposal,
                        _field_name: str = field_name,
                    ) -> list[MetricComparison]:
                        return run_feature_window_backtest(
                            bars_df,
                            field_name=_field_name,
                            champion_window=max(2, round(proposal.champion_value)),
                            challenger_window=max(2, round(proposal.challenger_value)),
                            direction_model=direction_model,
                            features_cfg=self._settings.features,
                        )

                    try:
                        # closed_trade_count is deliberately NOT passed here.
                        # min_trades_between_attempts is a "has enough new
                        # evidence accumulated" guard, and for this group the
                        # evidence is bar history, not trades -- these
                        # parameters are evaluated by a bar-driven backtest.
                        # Gating them on trade flow would stall bar-driven
                        # tuning through any quiet period, and would repeat the
                        # cross-group coupling the note at the top of
                        # _attempt_all() exists to prevent.
                        result = runner.attempt(
                            param_name, evaluate_feature_window, primary_metric="oos_sharpe"
                        )
                        log.info(
                            "tuning.scheduler_attempt",
                            param=param_name,
                            attempted=result.attempted,
                            accepted=result.accepted,
                            promoted=result.promoted,
                            reasons=result.reasons,
                        )
                    except Exception as exc:
                        log.error(
                            "tuning.scheduler_attempt_error",
                            param=param_name,
                            error=str(exc),
                            exc_info=True,
                        )

            # XGBoost hyperparameters -- needs only bar history (it trains its
            # own champion/challenger models from scratch), but is expensive
            # enough to be throttled to once every `xgboost_cycle_interval`
            # cycles and run off the event loop via run_in_executor.
            if self._cycle_count % self._xgboost_cycle_interval != 0:
                log.info(
                    "tuning.scheduler_xgboost_cycle_skipped",
                    cycle=self._cycle_count,
                    interval=self._xgboost_cycle_interval,
                )
            else:
                try:
                    # Off-loop for the same reason the retrain below is: this
                    # module's own docstring says the point is that "a
                    # multi-second-to-minutes retrain does not" block the
                    # loop, but the feature build feeding it ran inline.
                    xgb_fm = await asyncio.to_thread(
                        build_feature_matrix, bars_df, cfg=self._settings.features
                    )
                except ValueError as exc:
                    log.info("tuning.scheduler_xgboost_feature_matrix_unavailable", error=str(exc))
                else:
                    loop = asyncio.get_running_loop()
                    for field_name in XGBOOST_HYPERPARAM_FIELDS:
                        param_name = f"xgboost.{field_name}"

                        def evaluate_xgb(
                            _param: TunableParameter,
                            proposal: Proposal,
                            _field_name: str = field_name,
                        ) -> list[MetricComparison]:
                            champion_value = proposal.champion_value
                            challenger_value = proposal.challenger_value
                            if _field_name in XGBOOST_INT_FIELDS:
                                champion_value = round(champion_value)
                                challenger_value = round(challenger_value)
                            return run_xgboost_hyperparam_backtest(
                                xgb_fm,
                                field_name=_field_name,
                                champion_value=champion_value,
                                challenger_value=challenger_value,
                                base_xgb_cfg=self._settings.xgboost,
                                symbol=self._symbol,
                                timeframe=self._timeframe,
                                feature_cfg=self._settings.features,
                            )

                        try:
                            result = await loop.run_in_executor(
                                None,
                                functools.partial(
                                    runner.attempt,
                                    param_name,
                                    evaluate_xgb,
                                    primary_metric="oos_sharpe",
                                ),
                            )
                            log.info(
                                "tuning.scheduler_attempt",
                                param=param_name,
                                attempted=result.attempted,
                                accepted=result.accepted,
                                promoted=result.promoted,
                                reasons=result.reasons,
                            )
                        except Exception as exc:
                            log.error(
                                "tuning.scheduler_attempt_error",
                                param=param_name,
                                error=str(exc),
                                exc_info=True,
                            )

    async def _maybe_retrain_e09(self) -> None:
        """Trigger E-09 walk-forward retrain every _e09_retrain_interval cycles (CRYPTO_BOX only)."""
        import os

        if self._cycle_count % self._e09_retrain_interval != 0:
            return
        if os.environ.get("CRYPTO_BOX", "").lower() not in ("1", "true", "yes"):
            return
        try:
            from src.tuning.engine_backtest import retrain_e09_walkforward

            df = await self._build_feature_bars_df()
            if df is None or len(df) < 230:
                log.info("e09_retrain_skipped", reason="insufficient_bars")
                return
            n = await retrain_e09_walkforward(df, self._symbol)
            log.info("e09_retrain_complete", n_samples=n)
        except Exception as exc:
            log.error("e09_retrain_failed", error=str(exc), exc_info=True)

    def _load_direction_model(self) -> XGBClassifier | None:
        try:
            return ModelTrainer.load_direction(
                self._settings.storage.model_dir, self._symbol, self._timeframe
            )
        except FileNotFoundError:
            return None

    async def _build_feature_bars_df(self) -> pd.DataFrame | None:
        latest_ts = await self._storage.latest_bar_ts(self._symbol, self._timeframe)
        if latest_ts is None:
            return None
        bars = await self._storage.bars_before(
            self._symbol, self._timeframe, latest_ts, limit=_FEATURE_BAR_FETCH_LIMIT
        )
        if not bars:
            return None
        return pd.DataFrame(
            {
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            },
            index=[b.ts for b in bars],
        )

    async def _closed_trade_count(self) -> int | None:
        """
        Number of closed trades on record, or None when it cannot be read.

        None rather than 0 on failure: 0 would read as "no new evidence since
        the last attempt" and block tuning indefinitely on a storage hiccup,
        while the runner treats None as "this guard cannot claim a verdict"
        and falls back to the wall-clock cooldown alone.
        """
        try:
            trades = await self._storage.fetch_trades(
                symbol=self._symbol,
                trading_mode=self._settings.trading_mode.value,
                limit=1_000_000,
            )
            return len(trades)
        except Exception as exc:
            log.warning("tuning.closed_trade_count_failed", error=str(exc), exc_info=True)
            return None

    async def _build_slippage_samples(self) -> list[SlippageFillSample]:
        trades = await self._storage.fetch_trades(
            symbol=self._symbol,
            trading_mode=self._settings.trading_mode.value,
            limit=1000,
        )
        spread_bps = self._settings.risk.slippage_default_spread_bps
        samples: list[SlippageFillSample] = []
        for t in trades:
            if t.entry_price <= 0.0 or t.quantity <= 0.0:
                continue
            bars = await self._storage.bars_before(
                self._symbol, self._timeframe, t.entry_ts, limit=21
            )
            if not bars:
                continue
            reference_price = bars[-1].close
            if reference_price <= 0.0:
                continue
            history = bars[:-1] if len(bars) > 1 else bars
            adv_20d = sum(b.volume for b in history) / len(history)
            if adv_20d <= 0.0:
                continue
            samples.append(
                SlippageFillSample(
                    reference_price=reference_price,
                    fill_price=t.entry_price,
                    qty=t.quantity,
                    adv_20d=adv_20d,
                    spread_bps=spread_bps,
                    direction=t.direction,
                )
            )
        samples.reverse()
        return samples

    async def _build_ensemble_blend_samples(self) -> list[EnsembleBlendSample]:
        trades = await self._storage.fetch_trades(
            symbol=self._symbol,
            trading_mode=self._settings.trading_mode.value,
            limit=1000,
        )
        return ensemble_blend_samples_from_trades(trades)

    async def _build_trade_samples(self) -> list[TradeSample]:
        trades = await self._storage.fetch_trades(
            symbol=self._symbol,
            trading_mode=self._settings.trading_mode.value,
            limit=1000,
        )
        samples: list[TradeSample] = []
        for t in trades:
            if t.exit_price is None or t.entry_price <= 0.0:
                continue
            snap = await self._storage.regime_snapshot_before(
                self._symbol, self._timeframe, t.entry_ts
            )
            if snap is None:
                continue
            entropy = _shannon_entropy(snap.prob_ranging, snap.prob_trending, snap.prob_volatile)
            raw_return = (t.exit_price / t.entry_price - 1.0) * (1 if t.direction == 1 else -1)
            samples.append(TradeSample(entropy=entropy, raw_return=raw_return))
        # Oldest-first so fold construction (contiguous blocks) reflects
        # chronological order, matching the harness's fold-purging assumption.
        samples.reverse()
        return samples
