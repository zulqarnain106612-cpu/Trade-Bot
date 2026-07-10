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
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, cast

import pandas as pd
import structlog
from xgboost import XGBClassifier

from src.config import REGIME_VOLATILE, TIMEFRAME_SECONDS, Timeframe, get_settings
from src.data.fetcher import MarketDataFetcher
from src.data.storage import StorageBackend
from src.diagnostics.signal_debugger import get_degradation_tracker, get_drift_monitor
from src.diagnostics.trade_auditor import AuditRecord, get_auditor
from src.features.pipeline import (
    FEATURE_COLUMNS,
    build_feature_matrix,
    build_inference_features,
)
from src.intelligence.probabilistic_adapter import ProbabilisticMetricsAdapter as _ProbAdapter
from src.intelligence.providers.aggregator import (
    get_onchain_aware_aggregator as _get_intel_aggregator,
)
from src.models.trainer import ModelTrainer
from src.regime.detector import RegimeDetector, RegimePrediction
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
from src.risk.kelly import KellyResult, compute_position_size
from src.risk.slippage import SlippageModel
from src.strategies.filters import apply_all_strategy_filters
from src.strategies.position_sizing import estimate_daily_vol, recommend_position_notional


# Module-level adapter singleton -- stateless, safe to reuse across ticks.
_PROB_ADAPTER = _ProbAdapter()

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Minimum bars in storage before a signal can be generated
_MIN_BARS_FOR_SIGNAL: Final[int] = 300


# ---------------------------------------------------------------------------
# Signal result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalResult:
    """
    Complete output of one signal engine tick.

    tradeable     : True when all gates pass and meta-label says bet
    direction     : 1=long, 0=short (valid only when tradeable)
    p_long        : XGBoost P(long)
    p_bet         : meta-label P(bet)
    kelly_result  : position sizing (None when not tradeable)
    regime        : current regime prediction
    gate_result   : final gate evaluation result
    skip_reason   : human-readable reason when not tradeable
    """

    tradeable: bool
    direction: int
    p_long: float
    p_bet: float
    kelly_result: KellyResult | None
    regime: RegimePrediction | None
    gate_result: GateResult | None
    skip_reason: str


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
        storage: StorageBackend,
        fetcher: MarketDataFetcher,
        detector: RegimeDetector,
        direction_model: XGBClassifier,
        meta_model: XGBClassifier,
        trainer: ModelTrainer,
    ) -> None:
        self._symbol = symbol
        self._timeframe = timeframe
        self._storage = storage
        self._fetcher = fetcher
        self._detector = detector
        self._direction_model = direction_model
        self._meta_model = meta_model
        self._trainer = trainer
        self._cfg = get_settings()
        self._model_lock = asyncio.Lock()  # protects model hot-swap (fix #14)
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
    ) -> None:
        """
        Atomically replace all three model objects under the model lock.

        Prevents a tick from reading a mismatched (v2 direction, v1 meta) pair
        during a concurrent hot-swap.
        """
        async with self._model_lock:
            self._direction_model = direction_model
            self._meta_model = meta_model
            self._detector = detector
        self._log.info("signal_engine.models_swapped")

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

        Returns
        -------
        SignalResult — always returned; tradeable=False when any gate blocks.
        """
        _tick_start = time.monotonic()
        try:
            await self._fetcher.gap_fill(self._symbol, self._timeframe)
        except Exception as exc:
            self._log.error("signal.gap_fill_failed", error=str(exc))
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

        # 3-5. Build feature matrix once — reused for inference vec AND regime history.
        # SCAN2-007: prior code called build_feature_matrix + build_inference_features
        # separately, running fractional_differentiation + all rolling stats twice per tick.
        # Single call eliminates ~50% of hot-path feature pipeline CPU overhead.
        try:
            fm = build_feature_matrix(bars)
        except Exception as exc:
            self._log.error("signal.feature_matrix_failed", error=str(exc))
            return self._skip("feature_matrix_failed")

        if fm.features is None or len(fm.features) < 1:
            return self._skip("insufficient_features")

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
            self._log.debug("signal.ofi_fetch_failed", error=str(exc))
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
                self._log.debug("signal.intel_fetch_failed", error=str(_intel_exc))
                _intel_metrics_dict = {}

            # Probabilistic post-processing: replace deterministic scalars with
            # Bayesian-posterior estimates (ProbabilisticMetricsAdapter).
            # On any model error the adapter returns None and gates fail open.
            try:
                _p_inputs = _PROB_ADAPTER.process(_intel_metrics_dict)
                _exchange_stress = _p_inputs.exchange_stress_score
                _whale_ratio = _p_inputs.whale_buy_sell_ratio
            except Exception:
                _exchange_stress = None
                _whale_ratio = None

        vec = build_inference_features(
            bars,
            live_ofi=live_ofi,
            feature_matrix=fm,
            intelligence_metrics=_intel_metrics_dict if _intel_metrics_dict else None,
        )
        if vec is None:
            return self._skip("insufficient_features_with_ofi")

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

        regime: RegimePrediction | None = None
        if history_df is not None and detector.is_fitted():
            try:
                regime = detector.predict_current(history_df, lookback=100)
            except Exception as exc:
                self._log.error(
                    "signal.regime_failed_defaulting_volatile",
                    error=str(exc),
                )
                # Fail-safe: default to VOLATILE so regime gate blocks new positions
                # until detector recovers — never default to RANGING (least restrictive)

        # NEW-017: REGIME_VOLATILE imported at module level — no per-tick import overhead
        regime_state = regime.state if regime is not None else REGIME_VOLATILE
        direction, p_long = self._trainer.predict_direction(direction_model, vec)

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
                forecast=float(p_long * 2.0 - 1.0),  # map [0,1] p_long to [-1,+1] forecast
                daily_vol_pct=_daily_vol,
                avg_book_correlation=1.0 - correlation_scalar,  # higher correlation → more shrink
            )
            _notional_cap_usd = _carver_result["recommended"]
        except Exception as _carver_exc:
            self._log.warning("signal.carver_cap_failed", error=str(_carver_exc))
            _notional_cap_usd = None

        # 7. Kelly sizing (pre-gate — needed for position-size gate)
        kelly_result = compute_position_size(
            p_long=p_long,
            direction=direction,
            capital_usd=capital_usd,
            entry_price=float(bars["close"].iloc[-1]),
            avg_win_usd=avg_win_usd,
            avg_loss_usd=avg_loss_usd,
            regime_scalar=regime_scalar,
            correlation_scalar=correlation_scalar,
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
        _spread_for_slippage = _live_ob_spread_bps if _live_ob_spread_bps is not None else None
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
                self._log.warning("signal.slippage_estimate_failed", error=str(_slip_exc))

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
            )
            get_auditor().record(rec)
            get_degradation_tracker().record_prediction(p_long, _p_bet_ref[0])

        # 8. Risk gate stack
        slippage_gate_result = check_slippage_veto(
            expected_edge_bps=_expected_edge_bps,
            slippage=_slippage_estimate,
        )
        if not slippage_gate_result.passed:
            gate_result = slippage_gate_result
            _emit_audit("skipped", "slippage_negative_ev", kelly_result, gate_result)
            return self._skip("slippage_negative_ev")

        gate_ctx = RiskGateContext(
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

        # 9. Meta-label gate
        meta_label, p_bet = self._trainer.predict_meta(meta_model, vec, p_long)
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
            correlation_scalar_applied=round(
                correlation_scalar, 3
            ),  # GAP-005/015: IS applied (see kelly.py)
            hurst=round(cast("dict[str, float]", _filter_result["details"]).get("hurst", 0.5), 3),
        )

        # ── Cognitive Engine — mandatory evaluation (no bypass) ─────────────
        # All five validators run: Quant, Probability, Risk, Blockchain, Regime.
        # A single VETO kills the trade regardless of all prior gates passing.
        _hurst = float(cast("dict[str, float]", _filter_result["details"]).get("hurst", 0.5))
        _adv_20d = (
            float(bars["volume"].rolling(20).mean().iloc[-1]) if "volume" in bars.columns else 1.0
        )
        _cog_ctx = SignalContext(
            signal_id=f"{self._symbol}_{self._timeframe}_{int(time.monotonic()*1000)}",
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
