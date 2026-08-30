"""
Signal engine — per-timeframe signal computation pipeline.

On every tick for a given (symbol, timeframe):
  1. Gap-fill bars from exchange into storage
  2. Load recent bars, build feature vector
  3. Predict HMM regime
  4. Evaluate risk gates (regime, drawdown, consecutive losses)
  5. Run XGBoost direction classifier → P(long)
  6. Run meta-label gate → P(bet)
  7. Compute Kelly position size
  8. Check position-size gate
  9. Return SignalResult to orchestrator

The signal engine owns no state beyond its injected dependencies.
All state lives in storage or the executor.

Authority:
  - López de Prado (2018) AFML Ch.3-4 signal construction
  - Hamilton (1989) regime gate
  - Kelly (1956) position sizing
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import pandas as pd
import structlog
from xgboost import XGBClassifier

from src.api.metrics import regime_ensemble_failure_total
from src.config import REGIME_VOLATILE, TIMEFRAME_SECONDS, Timeframe, get_settings
from src.data.fetcher import MarketDataFetcher
from src.data.storage import AnyStorageBackend, ModelMetricsRecord
from src.diagnostics.audit_trail import get_audit_trail
from src.diagnostics.decision_log_writer import StructuralChangeRecord, append_to_decision_log
from src.diagnostics.signal_debugger import get_degradation_tracker, get_drift_monitor
from src.diagnostics.trade_auditor import AuditRecord, get_auditor
from src.features.pipeline import (
    COL_GARCH_VOL,
    FEATURE_COLUMNS,
    FeatureMatrix,
    build_feature_matrix,
    build_inference_features,
)
from src.intelligence.ensemble_predictor import EnsemblePredictor
from src.intelligence.probabilistic_adapter import ProbabilisticMetricsAdapter as _ProbAdapter
from src.intelligence.providers.aggregator import (
    get_onchain_aware_aggregator as _get_intel_aggregator,
)
from src.intelligence.risk_quantification import RiskQuantifier
from src.models.model_registry import ModelRegistry
from src.models.online_trainer import OnlineTrainer
from src.models.trainer import ModelTrainer
from src.regime.changepoint import BayesianOnlineChangepointDetector
from src.regime.detector import RegimeDetector, RegimePrediction
from src.regime.ensemble import RegimeEnsembleVote, combine_regime_votes
from src.risk.capital_preservation_floor import CapitalPreservationFloor
from src.risk.cognitive_engine import (
    SignalContext,
    get_cognitive_engine,
)
from src.risk.gates import (
    GateResult,
    RiskGateContext,
    check_slippage_veto,
    evaluate_all_gates,
)
from src.risk.kelly import KellyResult, apply_size_scalar, compute_position_size
from src.risk.macro_exposure_budget import (
    MacroExposureBudget,
    apply_macro_budget_to_kelly_fraction,
)
from src.risk.slippage import SlippageModel
from src.strategies.filters import apply_all_strategy_filters, ewm_trend_signal, mtf_trend_aligned
from src.strategies.position_sizing import (
    CARVER_FORECAST_SCALAR,
    estimate_daily_vol,
    recommend_position_notional,
)
from src.tuning.live_overrides import effective_risk_settings


# Module-level adapter singleton -- stateless, safe to reuse across ticks.
_PROB_ADAPTER = _ProbAdapter()

# UI-015: next-higher-timeframe pairing for mtf_trend_aligned (Schwager 1993).
# Timeframe.SWING ("4h") has no higher pair configured, so MTF confirmation
# is a no-op for it (falls through _MTF_SLOWER_TIMEFRAME.get() -> None).
_MTF_SLOWER_TIMEFRAME: Final[dict[Timeframe, Timeframe]] = {
    Timeframe.SCALPING: Timeframe.INTRADAY,  # 1m -> 15m
    Timeframe.INTRADAY: Timeframe.SWING,  # 15m -> 4h
}
_MTF_SLOW_BARS_LIMIT: Final[int] = 250
_EWM_SPAN_TREND_MIN: Final[int] = 200  # matches filters._EWM_SPAN_TREND default

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Minimum bars in storage before a signal can be generated
_MIN_BARS_FOR_SIGNAL: Final[int] = 300

# Bars of p_long history retained for online meta training. Comfortably
# exceeds any triple-barrier resolution horizon, so a bar's recorded
# probability is still present when its label finally resolves.
_P_LONG_HISTORY_BARS: Final[int] = 500


# ---------------------------------------------------------------------------
# v4 shadow-mode model promotion (src/models/model_registry.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShadowBundle:
    """
    A retrained model set awaiting promotion. The four objects are kept
    together because they were trained as a unit: the meta model is fitted
    against this direction model's outputs, so promoting a direction model
    without its meta partner produces exactly the mismatched pair the model
    lock exists to prevent.
    """

    model_id: str
    direction_model: Any
    meta_model: Any
    detector: Any
    ensemble: Any = None
    # Metrics rows for this bundle, written only if it is promoted. The live
    # gate (risk.gates.check_live_gate) reads the *latest* metrics row for the
    # timeframe, so inserting a candidate's row at training time would let a
    # model that is not trading decide whether live trading is permitted.
    metrics: tuple[ModelMetricsRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class _PendingShadowObservation:
    """
    A shadow/live prediction pair made on `bar_ts`, awaiting the next bar
    to reveal which one was right. Held rather than scored immediately
    because the outcome does not exist yet at prediction time.
    """

    model_id: str
    bar_ts: int
    close: float
    live_p_long: float
    shadow_p_long: float


def _append_decision_log(record: StructuralChangeRecord, path: Path) -> None:
    """
    Blocking decision-log append, run off-loop via asyncio.to_thread.
    Creates the parent directory because storage settings deliberately no
    longer create directories as a validator side effect (VUL-031).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    append_to_decision_log(record, path)


# ---------------------------------------------------------------------------
# Signal result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalResult:
    """
    Complete output of one signal engine tick.

    tradeable              : True when all gates pass and meta-label says bet
    direction              : 1=long, 0=short (valid only when tradeable)
    p_long                 : XGBoost P(long)
    p_bet                  : meta-label P(bet)
    kelly_result           : position sizing (None when not tradeable)
    regime                 : current regime prediction
    gate_result            : final gate evaluation result
    skip_reason            : human-readable reason when not tradeable
    regime_agreement_scalar: HMM/changepoint agreement [0.5, 1.0]; 1.0 means
                             full agreement, <1.0 means disagreement already
                             reduced the kelly_result notional proportionally.
    pre_blend_p_long       : XGBoost's P(long) *before* the ensemble blend, and
                             ensemble_p_long is the probability the blend mixed
                             in. Both None when no blend happened.
    ensemble_blend_weight  : the weight in force at signal time.

    The three blend fields exist so `risk.ensemble_blend_weight` can be
    recalibrated against realized outcomes: a candidate weight w gives
    ``(1-w)*pre_blend_p_long + w*ensemble_p_long`` directly, for any w, on
    every trade. Recording only the post-blend p_long would make that
    impossible — the value is perturbed again by the online-trainer blend
    immediately afterwards, so the pre-blend input cannot be recovered by
    inverting it.
    """

    tradeable: bool
    direction: int
    p_long: float
    p_bet: float
    kelly_result: KellyResult | None
    regime: RegimePrediction | None
    gate_result: GateResult | None
    skip_reason: str
    regime_agreement_scalar: float = 1.0
    changepoint_probability: float = 0.0
    # RiskSettings.ensemble_blend_weight — None when no ensemble predictor is
    # injected or the configured blend weight is 0.0 for this tick.
    ensemble_point_estimate: float | None = None
    ensemble_uncertainty: float | None = None
    ensemble_blend_weight: float | None = None
    # The blend's two inputs. Set together with ensemble_blend_weight or not
    # at all -- orchestrator._blend_audit treats a partial set as no blend.
    pre_blend_p_long: float | None = None
    ensemble_p_long: float | None = None


# ---------------------------------------------------------------------------
# SignalEngine
# ---------------------------------------------------------------------------


class SignalEngine:
    """
    Stateless per-timeframe signal computation engine.

    One instance per (symbol, timeframe).  Injected with fitted models
    and shared infrastructure objects by the orchestrator.

    Usage::

        engine = SignalEngine(
            symbol="BTC/USDT",
            timeframe=Timeframe.INTRADAY,
            storage=storage,
            fetcher=fetcher,
            detector=detector,
            direction_model=direction_model,
            meta_model=meta_model,
            trainer=trainer,
        )
        result = await engine.tick(
            capital_usd=1000.0,
            daily_pnl_usd=-5.0,
            starting_equity_usd=1000.0,
            consecutive_loss_count=1,
            direction_gate_pass=True,
            meta_gate_pass=True,
        )
    """

    def __init__(
        self,
        symbol: str,
        timeframe: Timeframe,
        storage: AnyStorageBackend,
        fetcher: MarketDataFetcher,
        detector: RegimeDetector,
        direction_model: XGBClassifier,
        meta_model: XGBClassifier,
        trainer: ModelTrainer,
        ensemble: EnsemblePredictor | None = None,
        online_trainer: OnlineTrainer | None = None,
    ) -> None:
        self._symbol = symbol
        self._timeframe = timeframe
        self._storage = storage
        self._fetcher = fetcher
        self._detector = detector
        self._direction_model = direction_model
        self._meta_model = meta_model
        self._trainer = trainer
        # Diversified prediction ensemble (ARIMA/XGBoost/LSTM/GP/TreeEnsemble),
        # trained by ModelTrainer.train_ensemble() alongside direction/meta.
        # None until the orchestrator's first retrain cycle produces one --
        # blending fails open to XGBoost-only p_long until then (see tick()).
        self._ensemble = ensemble
        # TASK-008 online learner. None disables the whole path -- the batch
        # models remain authoritative either way, since blend() applies a
        # fixed 0.15 weight and returns the batch values unchanged until it
        # has warmed up. Learning is the point; the blend is deliberately
        # small because this is a drift detector, not a replacement model.
        self._online_trainer = online_trainer
        # Bar timestamp of the most recent sample already fed to the online
        # learner. Ticks arrive faster than bars close, so without this the
        # same resolved bar would be learned repeatedly and the SGD model
        # would overweight whichever bar happened to sit at the boundary.
        self._last_online_learn_ts: int | None = None
        # Column order the online learner was fitted on. The live inference
        # vector carries extra intelligence columns that the historical
        # feature matrix does not, so blending has to project onto this list
        # or sklearn sees a different feature count than it was fitted with
        # and every blend fails open forever.
        self._online_feature_cols: list[str] | None = None
        # bar_ts -> the batch p_long this engine produced for that bar, kept so
        # the online META model can be trained on the same input it is scored
        # with. Bounded: only recent bars can still have an unresolved barrier,
        # so anything older is dead weight.
        self._p_long_by_bar: OrderedDict[int, float] = OrderedDict()
        self._cfg = get_settings()
        self._model_lock = asyncio.Lock()  # protects model hot-swap (fix #14)
        # v4 regime ensemble (observability only — never gates trades, see
        # tick()): a per-engine BOCPD instance must persist its run-length
        # distribution across ticks, so it lives on self rather than being
        # recreated per call.
        self._changepoint_detector = BayesianOnlineChangepointDetector()
        # Per-engine so its fitted-distribution caches key off this
        # symbol/timeframe's own return window rather than being shared
        # across engines that see different data.
        self._risk_quantifier = RiskQuantifier()
        # v10 capital preservation floor (src/risk/capital_preservation_floor.py):
        # whole-book peak-drawdown halt that never auto-clears on equity
        # recovery. One instance per engine, driven by this engine's own
        # capital_usd stream each tick — see tick() gate 0.
        self._capital_floor = CapitalPreservationFloor(
            max_drawdown_pct=self._cfg.risk.capital_preservation_max_drawdown_pct
        )
        # v4 shadow-mode promotion. One registry per engine, i.e. per
        # (symbol, timeframe) — deliberately not model_registry's process-wide
        # singleton, whose single live slot would conflate the 5m, 15m and 4h
        # models into one and let a 5m retrain evict the 4h incumbent.
        self._registry = ModelRegistry(min_evaluations=self._cfg.xgboost.shadow_min_evaluations)
        # The models handed to __init__ came off disk with no version attached;
        # "initial" names that honestly rather than inventing a timestamp. Every
        # later swap records the real training version.
        self._registry.set_live_model("initial")
        self._shadow: ShadowBundle | None = None
        self._pending_shadow: _PendingShadowObservation | None = None
        self._log = log.bind(
            component="signal_engine",
            symbol=symbol,
            timeframe=timeframe.value,
        )

    # ------------------------------------------------------------------
    # Atomic model swap — called by orchestrator after retraining (fix #14)
    # ------------------------------------------------------------------

    async def swap_models(
        self,
        direction_model: Any,
        meta_model: Any,
        detector: Any,
        ensemble: Any = None,
        model_id: str | None = None,
    ) -> None:
        """
        Atomically replace all model objects under the model lock.

        Prevents a tick from reading a mismatched (v2 direction, v1 meta) pair
        during a concurrent hot-swap. `ensemble` defaults to None so existing
        callers that don't yet train/pass one are unaffected — a tick with no
        ensemble simply falls back to XGBoost-only p_long (see tick()).

        Any shadow under evaluation is dropped: it was being scored against
        the model this call is replacing, so its accumulated comparison no
        longer answers the question "is this better than what is running?".
        """
        async with self._model_lock:
            self._direction_model = direction_model
            self._meta_model = meta_model
            self._detector = detector
            if ensemble is not None:
                self._ensemble = ensemble
            if model_id is not None:
                self._registry.set_live_model(model_id)
            self._discard_shadow_locked("live_model_replaced")
        self._log.info(
            "signal_engine.models_swapped",
            ensemble_swapped=ensemble is not None,
            model_id=model_id,
        )

    # ------------------------------------------------------------------
    # v4 shadow-mode promotion
    # ------------------------------------------------------------------

    async def set_shadow_bundle(self, bundle: ShadowBundle) -> None:
        """
        Puts a retrained bundle under live evaluation without giving it any
        influence over trading. Its predictions are computed each tick and
        scored against the incumbent's on the same bar; nothing it produces
        reaches sizing, gating, or order placement until promote-time.

        A newer candidate supersedes an older one: keeping both would mean
        the older shadow eventually promotes over a model that has already
        been superseded by fresher data.
        """
        async with self._model_lock:
            self._discard_shadow_locked("superseded_by_newer_candidate")
            self._shadow = bundle
            self._registry.register_shadow(bundle.model_id)
        self._log.info("signal_engine.shadow_registered", model_id=bundle.model_id)

    def _discard_shadow_locked(self, reason: str) -> None:
        """Drops the current shadow. Caller must hold `self._model_lock`."""
        if self._shadow is None:
            return
        model_id = self._shadow.model_id
        if model_id in self._registry.shadow_ids():
            self._registry.discard_shadow(model_id)
        self._shadow = None
        self._pending_shadow = None
        self._log.info("signal_engine.shadow_discarded", model_id=model_id, reason=reason)

    async def _evaluate_shadow_tick(
        self, bars: pd.DataFrame, vec: pd.Series, live_p_long: float
    ) -> None:
        """
        Scores the shadow bundle against the incumbent on this bar, and
        promotes or abandons it when the window closes.

        `live_p_long` must be the direction model's raw output, taken before
        the ensemble blend: the shadow is a direction model, so blending one
        side of the comparison and not the other would measure the ensemble,
        not the candidate.

        The comparison is lagged by one bar because the outcome of a
        prediction made at bar T only exists at bar T+1. Nothing here can
        influence this tick's signal — the shadow's prediction is recorded
        and discarded, never returned.
        """
        if not self._cfg.xgboost.shadow_mode_enabled:
            return

        promoted: tuple[str, float, float, int] | None = None
        pending_metrics: tuple[ModelMetricsRecord, ...] = ()
        async with self._model_lock:
            bundle = self._shadow
            if bundle is None:
                return
            model_id = bundle.model_id
            bar_ts = int(bars.index[-1])
            close = float(bars["close"].iloc[-1])

            # 1. Resolve the previous bar's pair, now that its outcome exists.
            pending = self._pending_shadow
            if pending is not None and pending.model_id != model_id:
                # Belt and braces: set_shadow_bundle already clears this.
                self._pending_shadow = None
            elif pending is not None and bar_ts > pending.bar_ts:
                if close != pending.close:
                    actual = 1 if close > pending.close else -1
                    self._registry.record_shadow_prediction(model_id, pending.shadow_p_long, actual)
                    self._registry.record_live_prediction_for_comparison(
                        model_id, pending.live_p_long, actual
                    )
                # An unchanged close has no direction to have predicted, so it
                # is dropped rather than scored — crediting it to "short"
                # would hand both models a coin flip that neither earned.
                self._pending_shadow = None

            # 2. Open this bar's pair.
            if self._pending_shadow is None:
                try:
                    _, shadow_p_long = self._trainer.predict_direction(bundle.direction_model, vec)
                except Exception as exc:
                    # A candidate that cannot score the live feature vector
                    # (e.g. a schema mismatch) can never be promoted, so it is
                    # dropped now instead of retrying every bar forever.
                    self._log.error(
                        "signal_engine.shadow_predict_failed",
                        model_id=model_id,
                        error=str(exc),
                        exc_info=True,
                    )
                    self._discard_shadow_locked("prediction_failed")
                    return
                self._pending_shadow = _PendingShadowObservation(
                    model_id=model_id,
                    bar_ts=bar_ts,
                    close=close,
                    live_p_long=live_p_long,
                    shadow_p_long=shadow_p_long,
                )

            # 3. Promote, abandon, or keep waiting.
            n_evals = self._registry.evaluation_count(model_id)
            ready, reason = self._registry.evaluate_shadow(model_id)
            if ready:
                shadow_acc, live_acc = self._registry.accuracies(model_id)
                self._registry.promote_shadow(model_id)
                self._direction_model = bundle.direction_model
                self._meta_model = bundle.meta_model
                self._detector = bundle.detector
                # Matches swap_models: a bundle trained without an ensemble
                # keeps the incumbent's rather than dropping to XGBoost-only.
                if bundle.ensemble is not None:
                    self._ensemble = bundle.ensemble
                self._shadow = None
                self._pending_shadow = None
                pending_metrics = bundle.metrics
                promoted = (model_id, shadow_acc, live_acc, n_evals)
            elif n_evals >= self._cfg.xgboost.shadow_max_evaluations:
                self._log.info(
                    "signal_engine.shadow_abandoned",
                    model_id=model_id,
                    evaluations=n_evals,
                    reason=reason,
                )
                self._discard_shadow_locked("did_not_beat_incumbent")

        if promoted is not None:
            # Publish the promoted bundle's metrics only now that it is the
            # model actually trading — see ShadowBundle.metrics.
            for metrics in pending_metrics:
                try:
                    await self._storage.insert_model_metrics(metrics)
                except Exception as exc:
                    self._log.error(
                        "signal_engine.promoted_metrics_insert_failed",
                        model_id=promoted[0],
                        model_name=metrics.model_name,
                        error=str(exc),
                        exc_info=True,
                    )
            await self._record_promotion(*promoted)

    async def _record_promotion(
        self, model_id: str, shadow_acc: float, live_acc: float, n_evals: int
    ) -> None:
        """
        Writes the promotion to the append-only decision log. Runs outside the
        model lock and never raises: the swap already happened, and a failed
        audit write must not be reported as a failed promotion.
        """
        self._log.info(
            "signal_engine.shadow_promoted",
            model_id=model_id,
            shadow_accuracy=shadow_acc,
            live_accuracy=live_acc,
            evaluations=n_evals,
        )
        record = StructuralChangeRecord(
            title=f"{self._symbol} {self._timeframe.value} model promoted",
            change_type="model_promoted",
            justification=(
                f"Shadow model {model_id} out-predicted the incumbent over "
                f"{n_evals} resolved live bars and was promoted to the live slot."
            ),
            evidence={
                "symbol": self._symbol,
                "timeframe": self._timeframe.value,
                "model_id": model_id,
                "shadow_accuracy": round(shadow_acc, 4),
                "live_accuracy": round(live_acc, 4),
                "evaluations": n_evals,
            },
        )
        path = self._cfg.storage.decision_log_path
        try:
            await asyncio.to_thread(_append_decision_log, record, path)
        except Exception as exc:
            self._log.error(
                "signal_engine.decision_log_write_failed",
                model_id=model_id,
                path=str(path),
                error=str(exc),
                exc_info=True,
            )

    async def shadow_status(self) -> dict[str, Any] | None:
        """Current shadow evaluation state, or None when nothing is under evaluation."""
        async with self._model_lock:
            if self._shadow is None:
                return None
            model_id = self._shadow.model_id
            shadow_acc, live_acc = self._registry.accuracies(model_id)
            ready, reason = self._registry.evaluate_shadow(model_id)
            return {
                "model_id": model_id,
                "evaluations": self._registry.evaluation_count(model_id),
                "min_evaluations": self._cfg.xgboost.shadow_min_evaluations,
                "max_evaluations": self._cfg.xgboost.shadow_max_evaluations,
                "shadow_accuracy": shadow_acc,
                "live_accuracy": live_acc,
                "ready_to_promote": ready,
                "reason": reason,
            }

    # ------------------------------------------------------------------
    # Main tick — called by orchestrator on every bar close
    # ------------------------------------------------------------------

    async def tick(
        self,
        capital_usd: float,
        daily_pnl_usd: float,
        starting_equity_usd: float,
        consecutive_loss_count: int,
        direction_gate_pass: bool,
        meta_gate_pass: bool,
        avg_win_usd: float = 0.0,
        avg_loss_usd: float = 0.0,
        paper_trading_days: int = 0,
        correlation_scalar: float = 1.0,
        macro_budget: MacroExposureBudget | None = None,
        drift_detector: Any = None,
    ) -> SignalResult:
        """
        Run one full signal computation cycle.

        Parameters
        ----------
        capital_usd           : current equity for position sizing
        daily_pnl_usd         : today's realized PnL for drawdown gate
        starting_equity_usd   : day-start equity for drawdown % calc
        consecutive_loss_count: trailing loss streak for consecutive gate
        direction_gate_pass   : direction model passed OOS live gate
        meta_gate_pass        : meta model passed OOS live gate
        avg_win_usd           : historical avg win for Kelly ratio
        avg_loss_usd          : historical avg loss for Kelly ratio
        correlation_scalar    : GAP-005/GAP-015 — portfolio correlation
                                 scalar in [0, 1] from
                                 PortfolioCorrelationTracker.correlation_scalar(),
                                 computed by the orchestrator across all
                                 tracked symbols' open positions. Defaults
                                 to 1.0 (no-op) — same backward-compatible
                                 contract as regime_scalar.
        drift_detector        : GAP-003 PerformanceDriftDetector, owned by the
                                 orchestrator (it is built after startup
                                 training, so it is passed per tick rather
                                 than at construction). None fails the drift
                                 gate open.
        macro_budget          : v7 portfolio-level macro overlay from
                                 src.risk.macro_exposure_budget, computed by
                                 the orchestrator. None = no macro data (or
                                 the overlay disabled), which is a no-op
                                 rather than a neutral shrink.

        Returns
        -------
        SignalResult — always returned; tradeable=False when any gate blocks.
        """
        _tick_start = time.monotonic()
        try:
            await self._fetcher.gap_fill(self._symbol, self._timeframe)
        except Exception as exc:
            self._log.error("signal.gap_fill_failed", error=str(exc), exc_info=True)
            return self._skip("gap_fill_failed")

        # 2. Load bars
        bars = await self._load_bars()
        if bars is None:
            return self._skip("insufficient_bars")

        # H-16: verify the last bar is a fully closed bar, not the currently forming one.
        # Kelly sizing uses bars["close"].iloc[-1] — a partial bar gives wrong notional.
        tf_ms = TIMEFRAME_SECONDS.get(self._timeframe, 60) * 1000
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        last_bar_ts_ms = int(bars.index[-1])
        if now_ms - last_bar_ts_ms < tf_ms:
            return self._skip("last_bar_not_yet_closed")

        # Resolve the previous tick's prediction now that a new bar has
        # closed — realized direction is the move between the last two
        # closed bars. Without this, ModelDegradationTracker.resolve_last()
        # is never invoked in production, leaving resolved_predictions/
        # accuracy (exposed via /status and the dashboard) permanently
        # null/dead despite record_prediction() firing every tick.
        # Tracker is keyed per-timeframe: Orchestrator runs one SignalEngine
        # per active_timeframe concurrently against the same primary_symbol,
        # and a shared global tracker let one timeframe's prediction get
        # resolved against another timeframe's just-closed bar.
        _tf_key = (
            self._timeframe.value if hasattr(self._timeframe, "value") else str(self._timeframe)
        )
        if len(bars) >= 2:
            _actual_direction = 1 if bars["close"].iloc[-1] > bars["close"].iloc[-2] else 0
            get_degradation_tracker(_tf_key).resolve_last(_actual_direction)

        # 3-5. Build feature matrix once — reused for inference vec AND regime history.
        # SCAN2-007: prior code called build_feature_matrix + build_inference_features
        # separately, running fractional_differentiation + all rolling stats twice per tick.
        # Single call eliminates ~50% of hot-path feature pipeline CPU overhead.
        # Off-loop: build_feature_matrix runs fractional differentiation, a
        # rolling GARCH forecast and triple-barrier labelling over the whole
        # bar window. The orchestrator already treats it as CPU-bound and
        # hands it to a dedicated executor for training ("Feature matrix —
        # CPU bound", NEW-002); running the same function inline here kept it
        # on the event loop on the HOT path.
        #
        # That loop is shared by all three timeframe tasks, the FastAPI
        # server, the position monitor and the order path, so a slow feature
        # build in the 1m tick stalled the 15m tick, the API and any order in
        # flight — not just this tick. Execution latency is a domain prior
        # here, and this was the largest synchronous block on the signal path.
        try:
            fm = await asyncio.to_thread(build_feature_matrix, bars)
        except Exception as exc:
            self._log.error("signal.feature_matrix_failed", error=str(exc), exc_info=True)
            return self._skip("feature_matrix_failed")

        if fm.features is None or len(fm.features) < 1:
            return self._skip("insufficient_features")

        self._learn_online(fm)

        # Inference vector — last row of feature matrix, augmented with live OFI.
        # TASK-010: co-fetch orderbook (spread_bps) + funding rate concurrently —
        # both are network I/O; running them in parallel avoids serial latency.
        _live_ob_spread_bps: float | None = None
        _live_funding_rate_8h: float = 0.0
        _exchange_stress: float | None = None  # None → intelligence gate fails open
        _whale_ratio: float | None = None
        _intel_metrics_dict: dict[str, float] = {}
        # Derive perp symbol: 'BTC/USDT' → 'BTC/USDT:USDT'
        _perp_sym = (self._symbol + ":USDT") if ":" not in self._symbol else self._symbol

        # TASK-010: phase-1 — fetch orderbook + funding rate concurrently.
        # These are independent of the intel aggregator so they always run,
        # even if the aggregator construction or fetch fails.
        try:
            _ob_res, _fr_res = await asyncio.gather(
                self._fetcher.fetch_orderbook(self._symbol),
                self._fetcher.fetch_funding_rate(_perp_sym),
                return_exceptions=True,
            )
            if isinstance(_fr_res, BaseException):
                self._log.debug("signal.funding_rate_fetch_failed", error=str(_fr_res))
            else:
                _live_funding_rate_8h = float(_fr_res)

            if isinstance(_ob_res, BaseException):
                raise _ob_res  # fall through to outer except for OFI fallback

            ob = _ob_res
            live_ofi = ob.order_flow_imbalance()
            mid = ob.mid_price
            if mid > 0.0:
                _live_ob_spread_bps = (ob.spread / mid) * 10_000.0
        except Exception as exc:
            # warning, not debug: this degrades order-flow signal quality and
            # (via the fields cleared below) fails the exchange-stress/whale
            # gates open -- an operator needs to see this by default, not
            # only when debug logging happens to be enabled.
            self._log.warning("signal.ofi_fetch_failed", error=str(exc), exc_info=True)
            live_ofi = None
            _exchange_stress = None
            _whale_ratio = None
            _intel_metrics_dict = {}
        else:
            # TASK-010: phase-2 — intel aggregator (optional, may degrade gracefully).
            # Wrapped separately so a broken aggregator/provider never resets funding_rate.
            try:
                # Multi-provider intelligence aggregator — Binance + OKX + CoinGecko +
                # blockchain.info.  Singleton; cache_ttl=300s (at most once per 5 min).
                _intel_agg = _get_intel_aggregator(
                    symbol=self._symbol,
                    perp_symbol=_perp_sym,
                )
                _intel_metrics_dict = await _intel_agg.fetch_metrics()
            except Exception as _intel_exc:
                self._log.warning("signal.intel_fetch_failed", error=str(_intel_exc), exc_info=True)
                _intel_metrics_dict = {}

            # Probabilistic post-processing: replace deterministic scalars with
            # Bayesian-posterior estimates (ProbabilisticMetricsAdapter).
            # On any model error the adapter returns None and gates fail open.
            try:
                _p_inputs = _PROB_ADAPTER.process(_intel_metrics_dict)
                _exchange_stress = _p_inputs.exchange_stress_score
                _whale_ratio = _p_inputs.whale_buy_sell_ratio
            except Exception as _prob_exc:
                self._log.warning(
                    "signal.probabilistic_adapter_failed", error=str(_prob_exc), exc_info=True
                )
                _exchange_stress = None
                _whale_ratio = None

        await self._persist_intelligence_features(bars, _intel_metrics_dict)

        vec = build_inference_features(
            bars,
            live_ofi=live_ofi,
            feature_matrix=fm,
            intelligence_metrics=_intel_metrics_dict if _intel_metrics_dict else None,
        )
        if vec is None:
            return self._skip("insufficient_features_with_ofi")

        # Extract GARCH vol early so it can feed both Kelly sizing and SignalContext.
        _garch_vol_early = (
            float(fm.features[COL_GARCH_VOL].iloc[-1])
            if COL_GARCH_VOL in fm.features.columns and fm.features[COL_GARCH_VOL].iloc[-1] > 0
            else 0.0
        )

        # ── Push live feature values to drift monitor (Aronson 2006) ──
        _drift_mon = get_drift_monitor()
        for feat_name, feat_val in vec.items():
            _drift_mon.push(str(feat_name), float(feat_val))

        # Regime history DataFrame — full feature matrix (>=50 rows required)
        history_df: pd.DataFrame | None = None
        if len(fm.features) >= 50:
            cols = [c for c in FEATURE_COLUMNS if c in fm.features.columns]
            if len(cols) >= 3:
                history_df = fm.features[cols]

        # 6. Direction prediction — read models under lock (fix #14)
        async with self._model_lock:
            direction_model = self._direction_model
            meta_model = self._meta_model
            detector = self._detector
            ensemble = self._ensemble

        regime: RegimePrediction | None = None
        if history_df is not None and detector.is_fitted():
            try:
                regime = detector.predict_current(history_df, lookback=100)
            except Exception as exc:
                self._log.error(
                    "signal.regime_failed_defaulting_volatile",
                    error=str(exc),
                    exc_info=True,
                )
                # Fail-safe: default to VOLATILE so regime gate blocks new positions
                # until detector recovers — never default to RANGING (least restrictive)

        # NEW-017: REGIME_VOLATILE imported at module level — no per-tick import overhead
        regime_state = regime.state if regime is not None else REGIME_VOLATILE

        # v4 regime ensemble — agreement_score now acts as a multiplicative
        # position-size scalar (1.0 = full agreement, 0.5 = half size floor).
        # Regime disagreement (HMM vs changepoint) signals genuine instability
        # that the HMM alone cannot detect mid-regime-transition; shrinking
        # the position is safer than a hard veto (Domain Prior: treat HMM
        # transitions as probabilistic, avoid hard-coded regime logic).
        _regime_agreement_scalar: float = 1.0  # default: no adjustment
        _cp_prob: float = 0.0
        if regime is not None and len(bars) >= 2:
            try:
                last_return = float(bars["close"].iloc[-1] / bars["close"].iloc[-2] - 1.0)
                cp_prob = self._changepoint_detector.update(last_return)
                _cp_prob = cp_prob
                ensemble_result = combine_regime_votes(
                    RegimeEnsembleVote(
                        hmm_prob_trending=regime.prob_trending,
                        hmm_prob_ranging=regime.prob_ranging,
                        hmm_prob_volatile=regime.prob_volatile,
                        changepoint_probability=cp_prob,
                    )
                )
                # Floor at 0.5: never zero-out from agreement alone (that's the
                # regime gate's job); instead reduce to 50% on total disagreement.
                _regime_agreement_scalar = max(0.5, ensemble_result.agreement_score)
                if ensemble_result.agreement_score < 0.5:
                    self._log.warning(
                        "signal.regime_ensemble_disagreement",
                        agreement_score=ensemble_result.agreement_score,
                        changepoint_probability=cp_prob,
                        regime_state=regime_state,
                        position_scalar=_regime_agreement_scalar,
                    )
            except Exception as exc:
                regime_ensemble_failure_total.inc()
                self._log.warning("signal.regime_ensemble_failed", error=str(exc), exc_info=True)
        direction, p_long = self._trainer.predict_direction(direction_model, vec)

        # v4 shadow-mode evaluation — scores any candidate bundle against the
        # incumbent on this bar using the raw, pre-blend p_long. Observational
        # only: it can swap the models used by *later* ticks, never this one.
        # Failing here must not cost a live signal, so it fails open.
        try:
            await self._evaluate_shadow_tick(bars, vec, p_long)
        except Exception as exc:
            self._log.warning("signal.shadow_eval_failed", error=str(exc), exc_info=True)

        # Ensemble blend (src/intelligence/ensemble_predictor.py) — conservative
        # by default (RiskSettings.ensemble_blend_weight, self-tunable via
        # risk.ensemble_blend_weight). Fails open to XGBoost-only p_long/direction
        # on any error or when no ensemble has been trained yet (ensemble is None
        # until the orchestrator's first retrain cycle produces and hot-swaps one).
        _ensemble_blend_weight = effective_risk_settings(self._cfg.risk).ensemble_blend_weight
        _ensemble_point_estimate: float | None = None
        # Kept alongside the point estimate so the closed trade can carry both
        # into run_ensemble_blend_backtest, which replays realized outcomes
        # against challenger blend weights.
        _ensemble_uncertainty: float | None = None
        # The two inputs to the blend itself. Recorded because the blended
        # p_long cannot be inverted back to them: the online-trainer blend
        # below perturbs it again immediately.
        _pre_blend_p_long: float | None = None
        _ensemble_p_long: float | None = None
        if ensemble is not None and _ensemble_blend_weight > 0.0:
            try:
                _ens_pred = ensemble.predict_row(vec)
                _ensemble_point_estimate = _ens_pred.point_estimate
                _total_uncertainty = math.sqrt(
                    _ens_pred.aleatoric_uncertainty**2 + _ens_pred.epistemic_uncertainty**2
                )
                _ensemble_uncertainty = _total_uncertainty
                # tanh of the ensemble's own signal-to-noise ratio: bounded in
                # (0, 1), symmetric around p=0.5, self-normalising against the
                # ensemble's own uncertainty rather than an arbitrary scale
                # constant. point_estimate is on the same log-return scale
                # fm.log_returns already uses elsewhere in this trainer.
                _z = _ensemble_point_estimate / max(_total_uncertainty, 1e-9)
                _p_ensemble_long = 0.5 * (1.0 + math.tanh(_z))
                _pre_blend_p_long = p_long
                _ensemble_p_long = _p_ensemble_long
                p_long = (
                    1.0 - _ensemble_blend_weight
                ) * p_long + _ensemble_blend_weight * _p_ensemble_long
                # Re-derive direction from the blended p_long — predict_direction's
                # own threshold rule (p_long >= 0.5 -> long), so a large enough
                # ensemble disagreement can flip direction, not just scale size.
                direction = 1 if p_long >= 0.5 else 0
            except Exception as exc:
                self._log.warning("signal.ensemble_blend_failed", error=str(exc), exc_info=True)
                _ensemble_point_estimate = None
                _ensemble_uncertainty = None
                _pre_blend_p_long = None
                _ensemble_p_long = None

        # TASK-008 online blend, applied here rather than once at the end:
        # p_long is final at this point and is what every gate downstream
        # reads, whereas p_bet does not exist until predict_meta runs. Each
        # output is consumed where its batch input is authoritative.
        self._record_p_long_for_bar(bars, p_long)

        p_long, _, _online_weight = self._blend_online(p_long, 0.5, vec)
        # Same rule as the ensemble blend above: re-derive direction from the
        # blended probability so a large enough disagreement flips the trade,
        # rather than silently leaving direction pointing the other way.
        direction = 1 if p_long >= 0.5 else 0

        # GAP-002: HMM posterior entropy gate — scale position size down when
        # the regime classification is uncertain (near-uniform posterior).
        # No fitted regime (regime is None) is already routed to
        # REGIME_VOLATILE above and will be blocked by the regime risk gate
        # before sizing matters, but default to 1.0 (no scaling) here rather
        # than re-deriving that fail-safe — single source of truth for the
        # fail-safe lives in the regime gate, not in sizing.
        regime_scalar = regime.position_scalar() if regime is not None else 1.0

        # 7a. Carver/AFML/Thorp notional cap (GAP-015 position_sizing.py wiring)
        # Carver (2019): 'whichever method gives the smaller position' — compute
        # the min of four sizing methods and use it as a hard cap on Kelly output.
        # Fails safe to None (no cap) on any error so Kelly sizing still runs.
        _notional_cap_usd: float | None = None
        try:
            _entry_price = float(bars["close"].iloc[-1])
            _daily_vol = estimate_daily_vol(bars["close"].to_numpy())
            _win_prob = min(max(p_long if direction == 1 else (1.0 - p_long), 0.01), 0.99)
            _wl_ratio = (
                (avg_win_usd / avg_loss_usd) if avg_win_usd > 0 and avg_loss_usd > 0 else 1.5
            )
            _carver_result = recommend_position_notional(
                capital_usd=capital_usd,
                price=_entry_price,
                p_long=p_long,
                win_prob=_win_prob,
                win_loss_ratio=_wl_ratio,
                # p_long maps to [-1,+1], but carver_forecast_position expects
                # a Carver-normalised forecast (E|f| = CARVER_FORECAST_SCALAR)
                # and divides by that same constant. Passing the raw [-1,+1]
                # value shrank the Carver leg by 10x, which made it the binding
                # minimum on essentially every trade and pinned this cap near
                # 0.75% of capital no matter how strong the edge — the "min of
                # methods" cap was really "Carver, undersized". Scale into the
                # units the sizer documents.
                forecast=float((p_long * 2.0 - 1.0) * CARVER_FORECAST_SCALAR),
                daily_vol_pct=_daily_vol,
                avg_book_correlation=1.0 - correlation_scalar,  # higher correlation → more shrink
            )
            _notional_cap_usd = _carver_result["recommended"]
        except Exception as _carver_exc:
            self._log.warning("signal.carver_cap_failed", error=str(_carver_exc), exc_info=True)
            _notional_cap_usd = None

        # CVaR notional ceiling. Kelly sizes from win probability and payoff
        # ratio and is blind to tail SHAPE, so a fat-tailed regime can clear
        # every Kelly check and still ruin the book. Composed with the Carver
        # cap by taking the tighter of the two -- both are ceilings, and the
        # binding one is whichever says less.
        _cvar_cap_usd = self._cvar_notional_cap(bars, capital_usd)
        if _cvar_cap_usd is not None:
            _notional_cap_usd = (
                _cvar_cap_usd
                if _notional_cap_usd is None
                else min(_notional_cap_usd, _cvar_cap_usd)
            )

        # 7. Kelly sizing (pre-gate — needed for position-size gate)
        # Combine portfolio-correlation scalar with regime-agreement scalar so
        # HMM/changepoint disagreement reduces size proportionally rather than
        # being logged and discarded (auditor finding #2).
        combined_scalar = correlation_scalar * _regime_agreement_scalar

        # v7 macro overlay. compute_position_size multiplies the Kelly
        # fraction by correlation_scalar, so shrinking the scalar here is
        # exactly equivalent to shrinking the resulting Kelly fraction --
        # apply_macro_budget_to_kelly_fraction is applied to the scalar for
        # that reason, and keeps the "budget can only shrink" invariant in
        # one place. A failure leaves combined_scalar untouched: losing a
        # ceiling must never be worse than never having had one.
        # Snapshotted rather than read again at log time: the tradeable log
        # line runs outside this guard, so re-reading the budget there would
        # let the same fault escape the handler that was written to contain it.
        _macro_scalar: float | None = None
        _macro_reason: str | None = None
        if macro_budget is not None:
            try:
                _macro_scalar = round(macro_budget.scalar, 3)
                _macro_reason = macro_budget.reason
                combined_scalar = apply_macro_budget_to_kelly_fraction(
                    combined_scalar, macro_budget
                )
            except Exception as _macro_exc:
                self._log.error(
                    "signal.macro_budget_apply_failed",
                    error=str(_macro_exc),
                    retained_scalar=round(combined_scalar, 4),
                    exc_info=True,
                )

        # GARCH vol-targeting scalar (Carver 2019): scale position inversely with
        # realized conditional vol when it exceeds the configured threshold. This
        # reduces notional exposure in high-vol regimes without a hard veto, keeping
        # trades alive at reduced size rather than blocking them entirely.
        _garch_threshold = effective_risk_settings(self._cfg.risk).garch_vol_threshold
        _garch_vol_scalar = (
            min(1.0, _garch_threshold / _garch_vol_early)
            if _garch_vol_early > _garch_threshold
            else 1.0
        )

        kelly_result = compute_position_size(
            p_long=p_long,
            direction=direction,
            capital_usd=capital_usd,
            entry_price=float(bars["close"].iloc[-1]),
            avg_win_usd=avg_win_usd,
            avg_loss_usd=avg_loss_usd,
            regime_scalar=regime_scalar,
            correlation_scalar=combined_scalar,
            garch_vol_scalar=_garch_vol_scalar,
            notional_cap_usd=_notional_cap_usd,
        )

        notional = kelly_result.notional_usd if kelly_result is not None else 0.0
        quantity = kelly_result.quantity if kelly_result is not None else 0.0

        # TASK-009: Compute slippage estimate and expected_edge_bps so gate 0 is active.
        # expected_edge_bps: p_long-derived edge using modelled win/loss payoff ratio.
        #   win_loss_ratio = avg_win_usd / avg_loss_usd (Kelly inputs reused here).
        #   Edge = p_win * avg_win - p_loss * avg_loss, expressed in bps relative to entry.
        _entry_price = float(bars["close"].iloc[-1])
        _wl_ratio = (avg_win_usd / avg_loss_usd) if avg_loss_usd > 0.0 else 1.0
        _p_win = p_long if direction == 1 else (1.0 - p_long)
        _p_loss = 1.0 - _p_win
        _raw_edge_usd = _p_win * avg_win_usd - _p_loss * avg_loss_usd
        _expected_edge_bps = (
            _raw_edge_usd / (_entry_price * quantity) * 10_000.0
            if quantity > 0.0 and _entry_price > 0.0
            else float((p_long - 0.5) * 200)
        )  # fallback: p_long proxy

        # TASK-009: Build SlippageEstimate using live spread (TASK-010) + ADV-20 from bars.
        # SlippageModel wants the *half*-spread — a single marketable order
        # crosses from mid to one touch, not touch to touch, and the config
        # fallback (slippage_default_spread_bps) is documented in that unit.
        # _live_ob_spread_bps is the full quoted width, so halve it here; the
        # unhalved value stays the one persisted to the trade audit trail.
        _spread_for_slippage = (
            _live_ob_spread_bps / 2.0 if _live_ob_spread_bps is not None else None
        )
        _adv_20d_for_slip = (
            float(bars["volume"].rolling(20).mean().iloc[-1]) if "volume" in bars.columns else None
        )
        _slippage_estimate = None
        if (
            _spread_for_slippage is not None
            and _adv_20d_for_slip is not None
            and _adv_20d_for_slip > 0.0
        ):
            try:
                _slippage_estimate = SlippageModel().estimate(
                    symbol=self._symbol,
                    qty=quantity,
                    price=_entry_price,
                    adv_20d=_adv_20d_for_slip,
                    spread_bps=_spread_for_slippage,
                )
            except Exception as _slip_exc:
                self._log.warning(
                    "signal.slippage_estimate_failed", error=str(_slip_exc), exc_info=True
                )

        # ── Audit closure setup — must precede all early-exit gates ──
        _prob_ranging = regime.prob_ranging if regime else 0.33
        _prob_trending = regime.prob_trending if regime else 0.33
        _prob_volatile = regime.prob_volatile if regime else 0.34
        _feat_dict = {str(k): float(v) for k, v in vec.items()}
        _p_bet_ref: list[float] = [0.0]  # updated after meta-label prediction

        def _emit_audit(
            outcome: str, skip: str, kr: KellyResult | None, gr: GateResult | None
        ) -> None:
            latency_ms = (time.monotonic() - _tick_start) * 1000
            rec = AuditRecord(
                ts_utc=time.time(),
                symbol=self._symbol,
                timeframe=self._timeframe.value
                if hasattr(self._timeframe, "value")
                else str(self._timeframe),
                features=_feat_dict,
                p_long=p_long,
                p_bet=_p_bet_ref[0],
                direction=direction,
                regime_state=regime_state,
                prob_ranging=_prob_ranging,
                prob_trending=_prob_trending,
                prob_volatile=_prob_volatile,
                gate_status=gr.status.value if gr else "unknown",
                gate_reason=gr.reason if gr else "",
                gate_details=gr.details if gr else {},
                kelly_fraction=kr.adjusted_fraction if kr else None,
                kelly_notional_usd=kr.notional_usd if kr else None,
                kelly_quantity=kr.quantity if kr else None,
                kelly_is_capped=kr.is_capped if kr else None,
                outcome=outcome,
                trade_id=None,
                skip_reason=skip,
                tick_latency_ms=round(latency_ms, 2),
                equity_usd_at_decision=capital_usd,
                ensemble_point_estimate=_ensemble_point_estimate,
                ensemble_uncertainty=_ensemble_uncertainty,
                ensemble_blend_weight=(
                    _ensemble_blend_weight if _ensemble_point_estimate is not None else None
                ),
            )
            get_auditor().record(rec)
            get_degradation_tracker(_tf_key).record_prediction(p_long, _p_bet_ref[0])

            # v8: same event, additionally appended to the hash-chained,
            # tamper-evident AuditTrail (src/diagnostics/audit_trail.py).
            # This never replaces TradeAuditor above -- TradeAuditor is the
            # rich, queryable per-tick record; AuditTrail is the compact,
            # append-only compliance ledger that can prove after the fact
            # that no entry was altered or removed.
            get_audit_trail().record(
                event_type=outcome,
                reason_code=skip or (gr.status.value if gr else "unknown"),
                details={
                    "symbol": self._symbol,
                    "timeframe": self._timeframe.value
                    if hasattr(self._timeframe, "value")
                    else str(self._timeframe),
                    "direction": direction,
                    "p_long": round(p_long, 6),
                    "gate_status": gr.status.value if gr else "unknown",
                    "kelly_fraction": kr.adjusted_fraction if kr else None,
                    "notional_usd": kr.notional_usd if kr else None,
                    "equity_usd": capital_usd,
                },
            )

        # 8. Risk gate stack
        slippage_gate_result = check_slippage_veto(
            expected_edge_bps=_expected_edge_bps,
            slippage=_slippage_estimate,
        )
        if not slippage_gate_result.passed:
            gate_result = slippage_gate_result
            _emit_audit("skipped", "slippage_negative_ev", kelly_result, gate_result)
            return self._skip("slippage_negative_ev")

        # v10 capital preservation floor: mark the latest equity, then read
        # the (possibly newly-tripped) halt state into the gate stack. Once
        # halted it keeps returning False regardless of equity recovery.
        #
        # update_equity() rejects a non-finite mark rather than storing it,
        # because an `inf` would become the permanent peak and leave every
        # later drawdown computing to NaN — silently disabling the outermost
        # backstop for the life of the process. Skipping the tick here keeps
        # that fault from reaching the floor's state at all; a tick we cannot
        # size is one we must not trade.
        try:
            self._capital_floor.update_equity(capital_usd)
        except ValueError as exc:
            self._log.error(
                "signal.capital_floor_mark_rejected",
                capital_usd=capital_usd,
                error=str(exc),
            )
            _emit_audit("skipped", "invalid_equity_mark", kelly_result, None)
            return self._skip("invalid_equity_mark")
        gate_ctx = RiskGateContext(
            capital_preservation_halted=self._capital_floor.is_halted,
            daily_pnl_usd=daily_pnl_usd,
            starting_equity_usd=starting_equity_usd,
            consecutive_loss_count=consecutive_loss_count,
            regime_state=regime_state,
            notional_usd=notional,
            capital_usd=capital_usd,
            trading_mode=self._cfg.trading_mode,
            direction_gate_pass=direction_gate_pass,
            meta_gate_pass=meta_gate_pass,
            paper_trading_days=paper_trading_days,
            expected_edge_bps=_expected_edge_bps,
            slippage_estimate=_slippage_estimate,
            exchange_stress_score=_exchange_stress,  # GAP-015: None → fail-open
            whale_buy_sell_ratio=_whale_ratio,  # GAP-015: None → fail-open
            drift_detector=drift_detector,  # GAP-003: None → fail-open
        )
        gate_result = evaluate_all_gates(gate_ctx)

        if not gate_result.passed:
            _emit_audit("skipped", gate_result.status.value, None, gate_result)
            return SignalResult(
                tradeable=False,
                direction=direction,
                p_long=p_long,
                p_bet=0.0,
                kelly_result=None,
                regime=regime,
                gate_result=gate_result,
                skip_reason=gate_result.status.value,
            )

        if kelly_result is None:
            _emit_audit("skipped", "kelly_size_zero", None, gate_result)
            return self._skip("kelly_size_zero")

        # Advisory gates reduce size rather than vetoing. Applied here, after
        # the gate stack has run and after the position-size gate has judged
        # the UNREDUCED notional -- a gate that caps absolute exposure must
        # not be talked out of firing by a reduction applied before it looked.
        #
        # Requantised rather than merely rescaled: a shrunk order is a
        # different order and has to clear the exchange's minimums again.
        # None means the reduced size is below what the exchange accepts, and
        # the trade is skipped -- taking it at full size because the
        # reduction was inconvenient is the one outcome a ceiling must never
        # produce.
        if gate_result.size_scalar < 1.0:
            _scaled = apply_size_scalar(
                kelly_result,
                gate_result.size_scalar,
                kelly_result.entry_price,
            )
            if _scaled is None:
                _emit_audit("skipped", "advisory_scalar_below_minimum", kelly_result, gate_result)
                return self._skip("advisory_scalar_below_minimum")
            self._log.info(
                "signal.advisory_size_reduction",
                status=gate_result.status.value,
                scalar=gate_result.size_scalar,
                notional_before=round(kelly_result.notional_usd, 2),
                notional_after=round(_scaled.notional_usd, 2),
            )
            kelly_result = _scaled

        # 9. Meta-label gate
        meta_label, p_bet = self._trainer.predict_meta(meta_model, vec, p_long)
        # Second blend call, for p_bet only -- p_long above is already blended
        # and is passed through unchanged here.
        _, p_bet, _online_weight = self._blend_online(p_long, p_bet, vec)
        meta_label = 1 if p_bet >= 0.5 else 0
        _p_bet_ref[0] = p_bet  # make p_bet available to _emit_audit closure

        if meta_label == 0:
            _emit_audit("skipped", "meta_label_gate_skip", kelly_result, gate_result)
            return SignalResult(
                tradeable=False,
                direction=direction,
                p_long=p_long,
                p_bet=p_bet,
                kelly_result=kelly_result,
                regime=regime,
                gate_result=gate_result,
                skip_reason="meta_label_gate_skip",
            )

        # 10. Professional strategy filters (Carver, Chan, Peters, Elder, Schwager)
        _atr_series = None
        if "high" in bars.columns and "low" in bars.columns:
            _atr_series = (bars["high"] - bars["low"]).rolling(14).mean()
        _filter_result = apply_all_strategy_filters(
            close=bars["close"],
            volume=bars["volume"],
            atr_series=_atr_series if _atr_series is not None else bars["high"] - bars["low"],
            direction=direction,
            regime_state=regime_state,
            prob_trending=_prob_trending,
            prob_ranging=_prob_ranging,
            prob_volatile=_prob_volatile,
            open_price=float(bars["open"].iloc[-1]) if "open" in bars.columns else None,
            prev_close=float(bars["close"].iloc[-2]) if len(bars) >= 2 else None,
            high=bars["high"] if "high" in bars.columns else None,
            low=bars["low"] if "low" in bars.columns else None,
        )

        if not _filter_result["passes"]:
            _reason = "strategy_filter:" + ",".join(
                cast("list[str]", _filter_result["filters_failed"])
            )
            _emit_audit("skipped", _reason, kelly_result, gate_result)
            return SignalResult(
                tradeable=False,
                direction=direction,
                p_long=p_long,
                p_bet=p_bet,
                kelly_result=kelly_result,
                regime=regime,
                gate_result=gate_result,
                skip_reason=_reason,
            )

        # 10b. UI-015: multi-timeframe trend confirmation (Schwager 1993) —
        # opt-in via FeatureSettings.mtf_confirmation_enabled (default False).
        # Only take this timeframe's signal when the next-higher timeframe's
        # trend agrees with the proposed direction.
        if self._cfg.features.mtf_confirmation_enabled:
            _slow_tf = _MTF_SLOWER_TIMEFRAME.get(self._timeframe)
            if _slow_tf is not None:
                try:
                    _slow_tf_seconds = TIMEFRAME_SECONDS.get(_slow_tf, 3600)
                    _slow_cutoff_ts = int(
                        (datetime.now(tz=UTC).timestamp() - _MTF_SLOW_BARS_LIMIT * _slow_tf_seconds)
                        * 1000
                    )
                    _slow_records = await self._storage.fetch_bars(
                        symbol=self._symbol,
                        timeframe=_slow_tf.value,
                        since_ts=_slow_cutoff_ts,
                        limit=_MTF_SLOW_BARS_LIMIT,
                    )
                    if len(_slow_records) >= _EWM_SPAN_TREND_MIN:
                        _slow_close = pd.Series(
                            [r.close for r in _slow_records],
                            index=[r.ts for r in _slow_records],
                        ).sort_index()
                        _fast_signal = ewm_trend_signal(bars["close"])
                        _slow_signal = ewm_trend_signal(_slow_close)
                        if not mtf_trend_aligned(_fast_signal, _slow_signal, direction):
                            _reason = (
                                f"mtf_trend_misaligned:{self._timeframe.value}_vs_{_slow_tf.value}"
                            )
                            _emit_audit("skipped", _reason, kelly_result, gate_result)
                            return SignalResult(
                                tradeable=False,
                                direction=direction,
                                p_long=p_long,
                                p_bet=p_bet,
                                kelly_result=kelly_result,
                                regime=regime,
                                gate_result=gate_result,
                                skip_reason=_reason,
                            )
                except Exception as exc:
                    # Fails open like the other professional filters when
                    # their required inputs are unavailable (e.g. overnight
                    # gap filter skips without open_price/prev_close) --
                    # a data-fetch hiccup on the confirmation timeframe
                    # must not block an otherwise-valid signal.
                    self._log.warning(
                        "signal.mtf_confirmation_failed", error=str(exc), exc_info=True
                    )

        # GAP-008 FIX: filters.py returns a "scalar" (AFML Ch.17 probability-based regime
        # confidence). Previously this was applied multiplicatively on top of the entropy-
        # scalar already baked into Kelly via compute_position_size(regime_scalar=...) —
        # double-discounting regime uncertainty through two uncalibrated formulas.
        # Resolution (ADR, option a): the filter scalar is logged for observability but
        # NO LONGER multiplied onto kelly_result. Regime sizing is the sole domain of
        # detector.py's entropy gate → compute_position_size(). filters.py's regime signal
        # contributes to the pass/fail vote (line above) but not to notional re-scaling.
        _regime_scalar = float(cast("float", _filter_result.get("scalar", 1.0)))  # logged only

        self._log.info(
            "signal.tradeable",
            direction="long" if direction == 1 else "short",
            p_long=round(p_long, 4),
            p_bet=round(p_bet, 4),
            regime_state=regime_state,
            notional_usd=round(kelly_result.notional_usd, 2),
            kelly_fraction=round(kelly_result.adjusted_fraction, 4),
            regime_scalar_filter_logged_only=round(_regime_scalar, 3),  # GAP-008: not applied
            online_blend_weight=round(_online_weight, 3),
            correlation_scalar_applied=round(
                correlation_scalar, 3
            ),  # GAP-005/015: IS applied (see kelly.py)
            macro_exposure_scalar=_macro_scalar,
            macro_exposure_reason=_macro_reason,
            hurst=round(cast("dict[str, float]", _filter_result["details"]).get("hurst", 0.5), 3),
            ensemble_point_estimate=(
                round(_ensemble_point_estimate, 6) if _ensemble_point_estimate is not None else None
            ),
            # None, not the configured weight, when no estimate was produced:
            # run_ensemble_blend_backtest selects on "blending actually
            # happened", so a weight recorded against a trade that was never
            # blended would be replayed as a real sample.
            ensemble_blend_weight=(
                _ensemble_blend_weight if _ensemble_point_estimate is not None else None
            ),
            ensemble_uncertainty=(
                round(_ensemble_uncertainty, 6) if _ensemble_uncertainty is not None else None
            ),
        )

        # ── Cognitive Engine — mandatory evaluation (no bypass) ─────────────
        # All five validators run: Quant, Probability, Risk, Blockchain, Regime.
        # A single VETO kills the trade regardless of all prior gates passing.
        _hurst = float(cast("dict[str, float]", _filter_result["details"]).get("hurst", 0.5))
        _adv_20d = (
            float(bars["volume"].rolling(20).mean().iloc[-1]) if "volume" in bars.columns else 1.0
        )
        _garch_vol = _garch_vol_early  # already extracted above for Kelly sizing
        _cog_ctx = SignalContext(
            signal_id=f"{self._symbol}_{self._timeframe}_{int(time.monotonic() * 1000)}",
            symbol=self._symbol,
            timeframe=self._timeframe.value
            if hasattr(self._timeframe, "value")
            else str(self._timeframe),
            p_long=p_long,
            p_bet=p_bet,
            expected_edge_bps=_expected_edge_bps,  # TASK-009/GAP-011: real edge from avg_win/avg_loss
            regime_state=regime_state,
            regime_probs=[_prob_ranging, _prob_trending, _prob_volatile],
            hurst_exponent=_hurst,
            current_price=float(bars["close"].iloc[-1]),
            atr=float(_atr_series.iloc[-1]) if _atr_series is not None else 0.0,
            atr_median_20=float(_atr_series.rolling(20).median().iloc[-1])
            if _atr_series is not None
            else 1.0,
            realized_vol=float(bars["close"].pct_change().rolling(20).std().iloc[-1] * (252**0.5))
            if "close" in bars.columns
            else 0.01,
            adv_20d=_adv_20d,
            spread_bps=_live_ob_spread_bps if _live_ob_spread_bps is not None else 2.0,  # TASK-010
            capital_usd=capital_usd,
            daily_pnl_usd=daily_pnl_usd,
            open_positions=0,
            consecutive_losses=consecutive_loss_count,
            funding_rate_8h=_live_funding_rate_8h,  # TASK-010: live from fetcher (0.0 for spot)
            basis_pct=0.0,  # spot only; wire basis here for perp trading
            exchange_name="binance",
            proposed_qty=kelly_result.quantity,
            proposed_notional_usd=kelly_result.notional_usd,
            kelly_adjusted_fraction=kelly_result.adjusted_fraction,
            garch_vol_forecast=_garch_vol,
            regime_agreement_score=_regime_agreement_scalar,
        )
        _cog_decision = get_cognitive_engine().evaluate(_cog_ctx)
        if not _cog_decision.passed:
            _emit_audit(
                "skipped", f"cognitive_veto:{_cog_decision.veto_reason}", kelly_result, gate_result
            )
            return SignalResult(
                tradeable=False,
                direction=direction,
                p_long=p_long,
                p_bet=p_bet,
                kelly_result=kelly_result,
                regime=regime,
                gate_result=gate_result,
                skip_reason=f"cognitive_veto:{_cog_decision.veto_reason}",
            )
        # Apply cognitive engine's adjusted size (may be reduced by WARN results)
        if _cog_decision.adjusted_size_fraction < kelly_result.adjusted_fraction:
            from dataclasses import replace as _dc_replace2

            kelly_result = _dc_replace2(
                kelly_result,
                notional_usd=round(
                    kelly_result.notional_usd
                    * (
                        _cog_decision.adjusted_size_fraction
                        / max(kelly_result.adjusted_fraction, 1e-9)
                    ),
                    4,
                ),
                quantity=round(
                    kelly_result.quantity
                    * (
                        _cog_decision.adjusted_size_fraction
                        / max(kelly_result.adjusted_fraction, 1e-9)
                    ),
                    8,
                ),
            )
        # ─────────────────────────────────────────────────────────────────────

        _emit_audit("opened", "", kelly_result, gate_result)

        return SignalResult(
            tradeable=True,
            direction=direction,
            p_long=p_long,
            p_bet=p_bet,
            kelly_result=kelly_result,
            regime=regime,
            gate_result=gate_result,
            skip_reason="",
            regime_agreement_scalar=_regime_agreement_scalar,
            changepoint_probability=_cp_prob,
            ensemble_point_estimate=_ensemble_point_estimate,
            ensemble_uncertainty=_ensemble_uncertainty,
            ensemble_blend_weight=(
                _ensemble_blend_weight if _ensemble_point_estimate is not None else None
            ),
            pre_blend_p_long=_pre_blend_p_long,
            ensemble_p_long=_ensemble_p_long,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_bars(self) -> pd.DataFrame | None:
        """Load recent bars from storage as a DataFrame."""
        # VUL-SIGNAL-001: since_ts=0 caused a full table scan on every tick.
        # Compute a real cutoff aligned to the bars we actually need so the
        # (symbol, timeframe, ts ASC) index is used efficiently.
        # M-04: datetime already imported at module level — inline import removed.
        n_bars_needed = _MIN_BARS_FOR_SIGNAL + 200
        tf_seconds = TIMEFRAME_SECONDS.get(self._timeframe, 60)
        cutoff_ts = int((datetime.now(tz=UTC).timestamp() - n_bars_needed * tf_seconds) * 1000)
        records = await self._storage.fetch_bars(
            symbol=self._symbol,
            timeframe=self._timeframe.value,
            since_ts=cutoff_ts,
            limit=n_bars_needed,
        )
        if len(records) < _MIN_BARS_FOR_SIGNAL:
            self._log.warning(
                "signal.insufficient_bars",
                n_bars=len(records),
                min_required=_MIN_BARS_FOR_SIGNAL,
            )
            return None

        df = pd.DataFrame(
            {
                "open": [r.open for r in records],
                "high": [r.high for r in records],
                "low": [r.low for r in records],
                "close": [r.close for r in records],
                "volume": [r.volume for r in records],
                "quote_volume": [r.quote_volume for r in records],
                "taker_buy_vol": [r.taker_buy_vol for r in records],
            },
            index=[r.ts for r in records],
        )
        return df.sort_index()

    def _record_p_long_for_bar(self, bars: pd.DataFrame, batch_p_long: float) -> None:
        """
        Remember this bar's p_long so the online meta model can train on it.

        Records the BATCH (pre-online-blend) probability deliberately: feeding
        the blended value back in would let the online model learn from its
        own output, which drifts toward self-confirmation rather than toward
        the market.

        Bounded to _P_LONG_HISTORY_BARS. Only bars whose triple-barrier label
        is still unresolved can ever be looked up, so older entries are dead
        weight, and an unbounded dict here would grow for the life of the
        process.
        """
        try:
            if bars is None or bars.empty:
                return
            ts_key = int(pd.Timestamp(bars.index[-1]).value)
            self._p_long_by_bar[ts_key] = float(batch_p_long)
            self._p_long_by_bar.move_to_end(ts_key)
            while len(self._p_long_by_bar) > _P_LONG_HISTORY_BARS:
                self._p_long_by_bar.popitem(last=False)
        except Exception as exc:
            self._log.debug("signal.p_long_record_failed", error=str(exc), exc_info=True)

    def _learn_online(self, fm: FeatureMatrix) -> None:
        """
        Feed the newest *resolved* labelled bar to the online learner.

        The triple-barrier labeller cannot label the most recent bars — their
        barriers have not been touched yet — so `fm.labels` is shorter than
        `fm.features`. Learning from the last feature row would therefore pair
        a bar with some other bar's label. Only the last row that actually has
        a label is used, matched by index, never by position.

        Ticks arrive faster than bars close, so `_last_online_learn_ts` gates
        repeats: without it the same bar would be learned on every tick and
        the SGD model would overweight whichever bar sat at the boundary.

        Both online models are fed, not just the direction one. `blend()`
        warms up on `min(dir_samples, meta_samples) >= 50`, so feeding only
        the direction model would leave the meta counter at zero forever and
        the blend would never activate — the module would stay exactly as
        inert as before, just with a producer attached.

        The meta-label used here is "did this bar resolve at a barrier at
        all" (`|label| == 1`), which is why the 0-labels dropped from
        direction learning are still useful to the meta model: direction
        learns from resolved directional moves, meta learns which bars were
        worth betting on in the first place. This is a weaker meta-label than
        the batch trainer's (which conditions on the primary model having
        been right), but it needs no stored per-bar prediction history and no
        second inference against a model whose feature schema may not match
        the historical matrix.

        Entirely best-effort. The online model is a drift detector layered on
        top of the batch models; a failure here must never cost a tick.
        """
        trainer = self._online_trainer
        if trainer is None:
            return
        try:
            labels = fm.labels
            if labels is None or len(labels) < 1:
                return
            labelled = labels.dropna()
            if labelled.empty:
                return
            bar_ts = labelled.index[-1]
            if bar_ts not in fm.features.index:
                return

            ts_key = int(pd.Timestamp(bar_ts).value)
            if self._last_online_learn_ts == ts_key:
                return

            self._online_feature_cols = list(fm.features.columns)
            feature_vec = fm.features.loc[bar_ts].to_numpy(dtype=float)
            raw_label = float(labelled.iloc[-1])

            # Meta first, and unconditionally: it learns which bars resolved
            # at a barrier at all, so an untouched bar is a real 0 for it
            # rather than missing data. p_long=0.5 is the honest input --
            # this bar's live direction probability was not retained, and
            # inventing one would teach the meta model a fiction.
            resolved = 1 if abs(raw_label) == 1.0 else 0
            # Only train the meta model on a bar whose actual p_long we
            # recorded. Passing a constant here instead would train the model
            # on a feature that never varies and then score it with one that
            # does -- a train/serve skew, since blend() appends the live
            # batch_p_long at inference. Skipping costs warm-up time; faking
            # it costs correctness in a way nothing would report.
            recorded_p_long = self._p_long_by_bar.get(ts_key)
            if recorded_p_long is not None:
                trainer.learn_meta(feature_vec, p_long=recorded_p_long, label=resolved)

            # Triple-barrier labels are -1/0/+1; the online direction model is
            # a binary long/short classifier, so a 0 (barrier untouched, no
            # directional information) is dropped rather than coerced.
            if raw_label != 0.0:
                trainer.learn_direction(feature_vec, label=1 if raw_label > 0 else 0)
            self._last_online_learn_ts = ts_key
        except Exception as exc:
            self._log.debug("signal.online_learn_failed", error=str(exc), exc_info=True)

    def _blend_online(
        self, batch_p_long: float, batch_p_bet: float, vec: pd.Series
    ) -> tuple[float, float, float]:
        """
        Blend batch probabilities with the online learner's view.

        Returns (p_long, p_bet, applied_weight); the batch values come back
        unchanged when there is no learner or it has not warmed up, so callers
        can apply the result unconditionally.
        """
        trainer = self._online_trainer
        cols = self._online_feature_cols
        if trainer is None or cols is None:
            return batch_p_long, batch_p_bet, 0.0
        try:
            projected = vec.reindex(cols).fillna(0.0).to_numpy(dtype=float)
            prediction = trainer.blend(
                batch_p_long=batch_p_long,
                batch_p_bet=batch_p_bet,
                feature_vec=projected,
            )
            return prediction.p_long, prediction.p_bet, prediction.online_weight
        except Exception as exc:
            self._log.debug("signal.online_blend_failed", error=str(exc), exc_info=True)
            return batch_p_long, batch_p_bet, 0.0

    def _cvar_notional_cap(self, bars: pd.DataFrame, capital_usd: float) -> float | None:
        """
        Largest notional whose expected tail loss stays inside the CVaR budget.

        CVaR, not VaR: VaR answers "how bad is the 5th percentile", which says
        nothing about how much worse the other 5% get. A position sized to a
        VaR limit is sized to the least bad outcome it was trying to survive.

        Returns None -- meaning "no CVaR ceiling" -- when the limit is not
        configured, when there is too little history for the tail estimate to
        mean anything, or on any error. A ceiling that cannot be computed must
        not become a ceiling of zero: this composes with Kelly and the Carver
        cap, both of which are still in force.
        """
        limit_pct = self._cfg.risk.cvar_limit_pct
        if limit_pct is None:
            return None
        try:
            lookback = self._cfg.risk.cvar_lookback_bars
            closes = bars["close"].to_numpy(dtype=float)[-(lookback + 1) :]
            if len(closes) < 101:
                return None
            returns = closes[1:] / closes[:-1] - 1.0
            returns = returns[~pd.isna(returns)]
            if len(returns) < 100:
                return None

            result = self._risk_quantifier.value_at_risk(
                returns,
                confidence_level=self._cfg.risk.cvar_confidence,
                method="historical",
            )
            cvar = float(result["cvar"])
            # cvar is a negative return in the loss tail. A non-negative value
            # means the tail estimate found no loss at all, which is not a
            # licence to size without limit -- it means the estimate carries no
            # information, so no ceiling is published.
            if not math.isfinite(cvar) or cvar >= 0.0:
                return None
            return float(limit_pct) * capital_usd / abs(cvar)
        except Exception as exc:
            self._log.warning("signal.cvar_cap_failed", error=str(exc), exc_info=True)
            return None

    async def _persist_intelligence_features(
        self, bars: pd.DataFrame, metrics: dict[str, float]
    ) -> None:
        """
        Write this tick's intelligence metrics to intelligence_features_history.

        The engine fetches these from the provider aggregator on every tick and,
        until now, used them only for the current decision and dropped them.
        `store_intelligence_features()` existed on both storage backends and was
        called by exactly one thing: scripts/backfill_intelligence.py, run by
        hand. So in a live deployment the table stayed empty, which silently
        starves two consumers that expect it to be populated -- the trainer's
        intelligence feature matrix (GAP-015) and the v7 macro overlay.

        Keyed on the latest bar timestamp, so the several ticks that occur
        within one bar upsert the same row rather than accumulating duplicates.
        The aggregator caches for 300s anyway, so those writes mostly carry
        identical values.

        Best-effort: a storage failure here loses one bar of history, which is
        recoverable by backfill. Letting it break the tick would trade a
        recoverable data gap for a missed trading decision.
        """
        if not metrics or bars is None or bars.empty:
            return
        try:
            bar_ts = int(bars.index[-1])
            # "confidence" is the aggregator's own quality score for this
            # merge, not a feature -- it is stored in its own column, so it
            # must not also be written as one.
            features = {k: v for k, v in metrics.items() if k != "confidence"}
            if not features:
                return
            await self._storage.store_intelligence_features(
                symbol=self._symbol,
                timeframe=self._timeframe.value,
                bar_ts=bar_ts,
                features=features,
                confidence=float(metrics.get("confidence", 0.0)),
                source="live",
            )
        except Exception as exc:
            self._log.warning("signal.intel_persist_failed", error=str(exc), exc_info=True)

    def _skip(self, reason: str) -> SignalResult:
        self._log.debug("signal.skip", reason=reason)
        return SignalResult(
            tradeable=False,
            direction=0,
            p_long=0.5,
            p_bet=0.0,
            kelly_result=None,
            regime=None,
            gate_result=None,
            skip_reason=reason,
        )
