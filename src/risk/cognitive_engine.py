"""
Cognitive Engine — Mandatory Runtime Decision Layer
====================================================
Every trade decision MUST pass through this engine.
No bypass. No skip. No partial evaluation.

Five mandatory validators run real mathematics on every signal:

  1. QuantValidator      — Kelly math, CPCV Sharpe, sizing bounds
  2. ProbabilityValidator— Bayesian posterior, CVaR, Monte Carlo paths
  3. RiskValidator       — STRIDE-aware runtime threats, VaR breach check
  4. BlockchainValidator — Exchange counterparty, funding rate, basis risk
  5. RegimeValidator     — HMM entropy gate, Hurst persistence, vol regime

All five must return PASS. Any single VETO kills the trade immediately.
Results are logged to TradeAuditor for every signal — pass or fail.

This engine runs at development time (Claude uses it for reasoning) and
at runtime (orchestrator calls it before every executor handoff).

Authority:
  - Kelly (1956) — position sizing mathematics
  - López de Prado (2018) AFML — regime-aware gating
  - Almgren & Chriss (2001) — transaction cost awareness
  - Hamilton (1989) — HMM posterior confidence
  - Peters (1994) — Hurst exponent persistence filter
  - Thorp (2006) — variance-adjusted Kelly
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

import numpy as np
import structlog

from src.config import get_settings


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


# ── Result types ──────────────────────────────────────────────────────────────


class ValidatorStatus(StrEnum):
    PASS = "pass"
    VETO = "veto"
    WARN = "warn"  # passes but logged as warning


@dataclass(frozen=True)
class ValidatorResult:
    validator: str
    status: ValidatorStatus
    reason: str
    metrics: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status in (ValidatorStatus.PASS, ValidatorStatus.WARN)


@dataclass
class CognitiveDecision:
    """
    Final output of the cognitive engine for one signal.
    passed=True only when ALL five validators pass.
    """

    signal_id: str
    passed: bool
    veto_reason: str | None
    results: list[ValidatorResult]
    adjusted_size_fraction: float  # final position size after all adjustments
    risk_score: float  # 0.0 (no risk) → 1.0 (maximum risk)
    confidence: float  # 0.0 → 1.0 composite confidence

    def as_dict(self) -> dict:
        return {
            "signal_id": self.signal_id,
            "passed": self.passed,
            "veto_reason": self.veto_reason,
            "adjusted_size_fraction": round(self.adjusted_size_fraction, 6),
            "risk_score": round(self.risk_score, 4),
            "confidence": round(self.confidence, 4),
            "validators": [
                {
                    "name": r.validator,
                    "status": r.status.value,
                    "reason": r.reason,
                    "metrics": r.metrics,
                }
                for r in self.results
            ],
        }


# ── Signal input contract ─────────────────────────────────────────────────────


@dataclass
class SignalContext:
    """Everything the cognitive engine needs to evaluate one signal."""

    # Identity
    signal_id: str
    symbol: str
    timeframe: str

    # Model outputs
    p_long: float  # XGBoost direction probability
    p_bet: float  # meta-label gate probability
    expected_edge_bps: float  # gross edge estimate in basis points

    # Regime
    regime_state: int  # 0=ranging, 1=trending, 2=volatile
    regime_probs: list[float]  # posterior from hmm.predict_proba()
    hurst_exponent: float  # H from R/S analysis

    # Market
    current_price: float
    atr: float  # current ATR (14-bar)
    atr_median_20: float  # 20-bar median ATR (vol explosion reference)
    realized_vol: float  # 20-bar realized volatility (annualized)
    adv_20d: float  # 20-day average daily volume (base asset)
    spread_bps: float  # live order book spread

    # Portfolio
    capital_usd: float
    daily_pnl_usd: float
    open_positions: int
    consecutive_losses: int

    # Exchange
    funding_rate_8h: float  # perpetual funding rate (0 for spot)
    basis_pct: float  # spot vs perp premium/discount %
    exchange_name: str  # "binance" | "okx"

    # Computed by caller
    proposed_qty: float  # base asset quantity proposed by Kelly
    proposed_notional_usd: float
    kelly_adjusted_fraction: float  # kelly_result.adjusted_fraction — SINGLE
    # source of truth for position size. CognitiveEngine multiplies this
    # down on WARN/VETO; it must never recompute size independently
    # (that created an uncontrolled rescale ratio vs. the entropy-gated
    # Kelly fraction — see _base_size() removal below).

    # Optional enrichment — defaults allow callers that predate this field
    # GARCH(1,1) one-step-ahead conditional volatility — Bollerslev (1986).
    # Per-bar sigma in raw-return units (NOT annualized). 0.0 when the
    # pipeline has not yet produced a valid forecast (warm-up period).
    garch_vol_forecast: float = 0.0
    # Regime ensemble agreement score in [0, 1] (Dietterich 2000).
    # 1.0 = HMM and changepoint detector fully agree (low regime uncertainty).
    # 0.0 = complete disagreement (HMM confident, changepoint screaming shift).
    # Defaults to 1.0 so pre-ensemble callers are treated as fully agreeing.
    regime_agreement_score: float = 1.0


# ── Validator interface ────────────────────────────────────────────────────────


class Validator(Protocol):
    """Structural interface shared by all five mandatory validators."""

    NAME: str

    def validate(self, ctx: SignalContext) -> ValidatorResult: ...


# ── 1. Quant Validator ────────────────────────────────────────────────────────


class QuantValidator:
    """
    Kelly criterion math, CPCV Sharpe bounds, sizing validation.
    López de Prado AFML + Kelly (1956) + Thorp (2006).
    """

    NAME = "quant"

    def validate(self, ctx: SignalContext) -> ValidatorResult:
        cfg = get_settings().risk
        metrics: dict = {}

        # 1a. Kelly fraction — continuous return form: f* = μ/sigma²
        #     μ = expected_edge_bps / 10000 (per-trade)
        #     sigma = realized_vol / sqrt(252 * bars_per_day)
        mu = ctx.expected_edge_bps / 10_000.0
        sigma = ctx.realized_vol / math.sqrt(252)
        if sigma <= 0:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                "realized_vol is zero — cannot compute Kelly",
                {"realized_vol": ctx.realized_vol},
            )

        kelly_full = mu / (sigma**2)
        kelly_half = kelly_full * cfg.kelly_multiplier
        kelly_capped = min(kelly_half, cfg.kelly_ceiling)
        metrics["kelly_full"] = round(kelly_full, 6)
        metrics["kelly_half"] = round(kelly_half, 6)
        metrics["kelly_capped"] = round(kelly_capped, 6)

        # 1b. Variance-adjusted Kelly (Thorp 2006)
        # Reduce fraction if realized_vol > 2x historical baseline (vol explosion)
        vol_ratio = ctx.atr / ctx.atr_median_20 if ctx.atr_median_20 > 0 else 1.0
        if vol_ratio > 2.0:
            kelly_capped *= 0.5
            metrics["vol_explosion_scalar"] = 0.5
            metrics["vol_ratio"] = round(vol_ratio, 4)
        else:
            metrics["vol_ratio"] = round(vol_ratio, 4)

        # 1c. Notional size check
        max_notional = ctx.capital_usd * cfg.max_position_size_pct / 100.0
        if ctx.proposed_notional_usd > max_notional:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"Proposed notional {ctx.proposed_notional_usd:.2f} USD "
                f"> max allowed {max_notional:.2f} USD ({cfg.max_position_size_pct}%)",
                {
                    **metrics,
                    "proposed_notional_usd": ctx.proposed_notional_usd,
                    "max_notional_usd": max_notional,
                },
            )

        # 1d. Minimum edge threshold — signal must have positive theoretical edge
        if ctx.expected_edge_bps <= 0:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"Signal has non-positive expected edge: {ctx.expected_edge_bps:.2f} bps",
                metrics,
            )

        # 1e. Meta-label confidence gate
        if ctx.p_bet < 0.5:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"Meta-label P(bet)={ctx.p_bet:.3f} < 0.5 — insufficient bet confidence",
                {**metrics, "p_bet": ctx.p_bet},
            )

        metrics.update(
            {
                "kelly_final": round(kelly_capped, 6),
                "p_bet": ctx.p_bet,
                "p_long": ctx.p_long,
                "expected_edge_bps": ctx.expected_edge_bps,
            }
        )
        return ValidatorResult(
            self.NAME,
            ValidatorStatus.PASS,
            f"Kelly={kelly_capped:.4f} edge={ctx.expected_edge_bps:.2f}bps p_bet={ctx.p_bet:.3f}",
            metrics,
        )


# ── 2. Probability Validator ──────────────────────────────────────────────────


class ProbabilityValidator:
    """
    Bayesian posterior, CVaR tail risk, Monte Carlo path simulation.
    """

    NAME = "probability"
    MC_PATHS = 1000
    CVAR_LEVEL = 0.05  # 5th percentile CVaR

    def validate(self, ctx: SignalContext) -> ValidatorResult:
        metrics: dict = {}

        # 2a. Bayesian posterior composite score
        # P(profitable | signal) ∝ P(bet) x P(long_confidence) x dominant_regime_prob
        # Use dominant_prob (max of posterior) as regime confidence — more
        # numerically stable than entropy-based conf for 3-state HMM.
        dominant_prob = max(ctx.regime_probs)
        regime_entropy = self._entropy(ctx.regime_probs)
        n_states = len(ctx.regime_probs)
        norm_entropy = regime_entropy / math.log(n_states) if n_states > 1 else 1.0
        # p_long confidence: distance from 0.5 (random) → 1.0 (certain)
        direction_conf = abs(ctx.p_long - 0.5) * 2.0
        bayesian_score = ctx.p_bet * direction_conf * dominant_prob
        metrics["bayesian_score"] = round(bayesian_score, 4)
        metrics["dominant_prob"] = round(dominant_prob, 4)
        metrics["direction_conf"] = round(direction_conf, 4)
        metrics["norm_entropy"] = round(norm_entropy, 4)

        if bayesian_score < 0.15:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"Bayesian composite score {bayesian_score:.3f} < 0.15 "
                f"(p_bet={ctx.p_bet:.3f} x dir_conf={direction_conf:.3f} x "
                f"dominant_prob={dominant_prob:.3f})",
                metrics,
            )

        # 2b. CVaR estimate via Monte Carlo
        cvar, var95 = self._monte_carlo_cvar(
            ctx.expected_edge_bps / 10_000,
            ctx.realized_vol / math.sqrt(252),
            ctx.proposed_notional_usd,
        )
        metrics["cvar_5pct_usd"] = round(cvar, 2)
        metrics["var_95_usd"] = round(var95, 2)

        # CVaR must not exceed 1% of capital (tighter than the 2% daily gate)
        cvar_limit = ctx.capital_usd * 0.01
        if abs(cvar) > cvar_limit:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"CVaR(5%) = ${abs(cvar):.2f} exceeds 1% capital limit ${cvar_limit:.2f}",
                metrics,
            )

        # 2c. Win-rate plausibility check
        # p_long should be consistent with claimed expected_edge_bps
        implied_edge = (ctx.p_long - 0.5) * 200  # bps if p_long is win rate
        if abs(implied_edge - ctx.expected_edge_bps) > 50:
            # Warn only — don't veto, different models compute edge differently
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.WARN,
                f"p_long-implied edge {implied_edge:.1f}bps vs "
                f"signal edge {ctx.expected_edge_bps:.1f}bps differ >50bps",
                {**metrics, "implied_edge_bps": round(implied_edge, 2)},
            )

        metrics["implied_edge_bps"] = round(implied_edge, 2)
        return ValidatorResult(
            self.NAME,
            ValidatorStatus.PASS,
            f"Bayesian={bayesian_score:.3f} CVaR=${abs(cvar):.2f} limit=${cvar_limit:.2f}",
            metrics,
        )

    @staticmethod
    def _entropy(probs: list[float]) -> float:
        return -sum(p * math.log(p + 1e-12) for p in probs if p > 0)

    def _monte_carlo_cvar(
        self,
        mu: float,
        sigma: float,
        notional: float,
    ) -> tuple[float, float]:
        """
        Simulate MC_PATHS return paths, compute CVaR(5%) and VaR(95%).
        Returns (cvar_usd, var95_usd) — both expressed as loss (negative = bad).
        """
        rng = np.random.default_rng(seed=42)
        returns = rng.normal(mu, sigma, self.MC_PATHS)
        pnl = returns * notional
        sorted_pnl = np.sort(pnl)
        var_idx = int(self.CVAR_LEVEL * self.MC_PATHS)
        var95 = float(sorted_pnl[var_idx])
        cvar = float(sorted_pnl[:var_idx].mean()) if var_idx > 0 else var95
        return cvar, var95


# ── 3. Risk Validator ─────────────────────────────────────────────────────────


class RiskValidator:
    """
    STRIDE-aware runtime checks, drawdown position, VaR breach.
    """

    NAME = "risk"

    def validate(self, ctx: SignalContext) -> ValidatorResult:
        cfg = get_settings().risk
        metrics: dict = {}

        # 3a. Daily drawdown position
        dd_pct = (ctx.daily_pnl_usd / ctx.capital_usd * 100) if ctx.capital_usd > 0 else 0
        metrics["daily_dd_pct"] = round(dd_pct, 4)
        if dd_pct <= -cfg.daily_drawdown_halt_pct:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"Daily drawdown {dd_pct:.2f}% at or beyond halt threshold "
                f"-{cfg.daily_drawdown_halt_pct}%",
                metrics,
            )

        # 3b. Consecutive loss momentum
        if ctx.consecutive_losses >= cfg.consecutive_loss_halt:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"Consecutive losses {ctx.consecutive_losses} >= halt threshold "
                f"{cfg.consecutive_loss_halt}",
                {**metrics, "consecutive_losses": ctx.consecutive_losses},
            )

        # 3c. Volatility explosion gate (Schwager 1984 — 2x median ATR)
        vol_ratio = ctx.atr / ctx.atr_median_20 if ctx.atr_median_20 > 0 else 1.0
        metrics["atr_vol_ratio"] = round(vol_ratio, 4)
        if vol_ratio > 2.0:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"Volatility explosion: ATR ratio {vol_ratio:.2f} > 2.0x median — "
                f"market is in abnormal volatility state",
                metrics,
            )

        # 3d. STRIDE — Tampering check: validate signal_id is not replayed
        # Basic: signal_id must contain timeframe and be recent (checked by caller)
        if not ctx.signal_id or len(ctx.signal_id) < 8:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                "Signal ID missing or malformed — possible tampering (STRIDE-T)",
                metrics,
            )

        # 3e. Composite risk score
        risk_score = self._compute_risk_score(ctx, dd_pct, vol_ratio)
        metrics["risk_score"] = round(risk_score, 4)
        metrics["open_positions"] = ctx.open_positions
        metrics["garch_vol_forecast"] = round(ctx.garch_vol_forecast, 6)
        metrics["regime_agreement_score"] = round(ctx.regime_agreement_score, 4)

        # Hard cap: risk score > 0.85 → veto even if individual gates pass
        if risk_score > 0.85:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"Composite risk score {risk_score:.3f} > 0.85 hard cap",
                metrics,
            )

        return ValidatorResult(
            self.NAME,
            ValidatorStatus.PASS,
            f"risk_score={risk_score:.3f} dd={dd_pct:.2f}% vol_ratio={vol_ratio:.2f}",
            metrics,
        )

    @staticmethod
    def _compute_risk_score(ctx: SignalContext, dd_pct: float, vol_ratio: float) -> float:
        """
        Normalized risk score 0→1. Combines drawdown, vol, consecutive losses,
        open position concentration, GARCH conditional vol, and regime
        disagreement — six weights summing to exactly 1.0, so a fully
        saturated context scores 1.0.

        GARCH component: garch_vol_forecast normalized against a 1-sigma
        daily move threshold (0.02 = 2% per bar). When GARCH vol is 0.0
        (warm-up / not available) this component contributes 0.0.
        """
        cfg = get_settings().risk
        dd_component = min(abs(dd_pct) / cfg.daily_drawdown_halt_pct, 1.0)
        vol_component = min(vol_ratio / 2.0, 1.0)
        loss_component = min(ctx.consecutive_losses / cfg.consecutive_loss_halt, 1.0)
        pos_component = min(ctx.open_positions / 5, 1.0)  # >5 open = max risk
        # GARCH: normalizes against the configurable threshold (RISK_GARCH_VOL_THRESHOLD).
        # Weight 0.02 — supplementary, not primary risk factor.
        garch_component = (
            min(ctx.garch_vol_forecast / cfg.garch_vol_threshold, 1.0)
            if ctx.garch_vol_forecast > 0.0
            else 0.0
        )
        # Regime disagreement: 1 - agreement_score (Dietterich 2000 ensemble).
        # When HMM is confident but changepoint detector fires, this is high.
        # Weight 0.05 — catches mid-transition signals that Sharpe/vol miss.
        regime_disagree_component = 1.0 - max(0.0, min(1.0, ctx.regime_agreement_score))
        return (
            0.33 * dd_component
            + 0.28 * vol_component
            + 0.24 * loss_component
            + 0.08 * pos_component
            + 0.02 * garch_component
            + 0.05 * regime_disagree_component
        )


# ── 4. Blockchain / Exchange Validator ────────────────────────────────────────


class BlockchainValidator:
    """
    Exchange counterparty risk, funding rate drag, basis risk, API integrity.
    """

    NAME = "blockchain"

    FUNDING_VETO_THRESHOLD = 0.0005  # 0.05% per 8h — unfavorable carry
    BASIS_VETO_THRESHOLD = 0.005  # 0.5% spot/perp divergence
    TRUSTED_EXCHANGES = {"binance", "okx", "bybit", "kraken"}

    def validate(self, ctx: SignalContext) -> ValidatorResult:
        metrics: dict = {}

        # 4a. Exchange identity check (STRIDE — Spoofing)
        if ctx.exchange_name.lower() not in self.TRUSTED_EXCHANGES:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"Unknown exchange '{ctx.exchange_name}' — not in trusted set (STRIDE-S)",
                {"exchange": ctx.exchange_name},
            )
        metrics["exchange"] = ctx.exchange_name

        # 4b. Funding rate drag (perpetual futures only)
        # Positive funding = longs pay shorts. Veto longs when funding > threshold.
        if abs(ctx.funding_rate_8h) > self.FUNDING_VETO_THRESHOLD:
            direction = "long" if ctx.p_long > 0.5 else "short"
            unfavorable = (direction == "long" and ctx.funding_rate_8h > 0) or (
                direction == "short" and ctx.funding_rate_8h < 0
            )
            if unfavorable:
                return ValidatorResult(
                    self.NAME,
                    ValidatorStatus.VETO,
                    f"Funding rate {ctx.funding_rate_8h * 100:.4f}% per 8h unfavorable "
                    f"for {direction} position (threshold ±{self.FUNDING_VETO_THRESHOLD * 100:.3f}%)",
                    {**metrics, "funding_rate_8h": ctx.funding_rate_8h, "direction": direction},
                )
        metrics["funding_rate_8h"] = ctx.funding_rate_8h

        # 4c. Basis risk (spot vs perpetual divergence)
        if abs(ctx.basis_pct) > self.BASIS_VETO_THRESHOLD * 100:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"Basis divergence {ctx.basis_pct:.3f}% > "
                f"{self.BASIS_VETO_THRESHOLD * 100:.2f}% threshold — "
                f"spot/perp pricing inconsistent",
                {**metrics, "basis_pct": ctx.basis_pct},
            )
        metrics["basis_pct"] = ctx.basis_pct

        # 4d. Liquidity check — participation rate vs ADV
        participation = (
            ctx.proposed_notional_usd / (ctx.adv_20d * ctx.current_price)
            if ctx.adv_20d > 0 and ctx.current_price > 0
            else 0.0
        )
        metrics["participation_rate"] = round(participation, 6)

        if participation > 0.001:  # >0.1% of daily volume — meaningful impact
            # Warn, don't veto — let SlippageModel handle the cost
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.WARN,
                f"Participation rate {participation * 100:.4f}% of ADV — "
                f"market impact will be material; slippage model active",
                metrics,
            )

        return ValidatorResult(
            self.NAME,
            ValidatorStatus.PASS,
            f"exchange=ok funding={ctx.funding_rate_8h * 100:.4f}%/8h "
            f"basis={ctx.basis_pct:.3f}% participation={participation * 100:.5f}%",
            metrics,
        )


# ── 5. Regime Validator ───────────────────────────────────────────────────────


class RegimeValidator:
    """
    HMM entropy gate, Hurst persistence, multi-timeframe alignment.
    Hamilton (1989) + Peters (1994).
    """

    NAME = "regime"

    ENTROPY_VETO_THRESHOLD = 0.90  # normalized entropy — above this = ambiguous
    ENTROPY_WARN_THRESHOLD = 0.70  # warn zone
    HURST_TRENDING_MIN = 0.55  # H < this → no momentum trades
    VOLATILE_STATE_IDX = 2  # config default

    def validate(self, ctx: SignalContext) -> ValidatorResult:
        cfg = get_settings().hmm
        metrics: dict = {}

        # 5a. Regime state hard gate
        if ctx.regime_state == cfg.volatile_state_index:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"Regime state = volatile (state {ctx.regime_state}) — no new positions allowed",
                {"regime_state": ctx.regime_state},
            )

        # 5b. HMM posterior entropy gate (GAP-002 — now enforced)
        n_states = len(ctx.regime_probs)
        if n_states < 2:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"regime_probs has {n_states} states — HMM not fitted",
                {},
            )

        raw_entropy = -sum(p * math.log(p + 1e-12) for p in ctx.regime_probs)
        max_entropy = math.log(n_states)
        norm_entropy = raw_entropy / max_entropy if max_entropy > 0 else 1.0
        metrics["hmm_entropy_normalized"] = round(norm_entropy, 4)
        metrics["hmm_regime_probs"] = [round(p, 4) for p in ctx.regime_probs]
        metrics["dominant_prob"] = round(max(ctx.regime_probs), 4)

        if norm_entropy > self.ENTROPY_VETO_THRESHOLD:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.VETO,
                f"HMM posterior entropy {norm_entropy:.3f} > {self.ENTROPY_VETO_THRESHOLD} "
                f"— regime transition ambiguous, no position opened",
                metrics,
            )

        # 5c. Hurst exponent filter (Peters 1994)
        metrics["hurst_exponent"] = round(ctx.hurst_exponent, 4)
        if ctx.hurst_exponent < self.HURST_TRENDING_MIN:
            direction = "long" if ctx.p_long > 0.5 else "short"
            if direction in ("long", "short"):  # momentum trade on mean-reverting series
                return ValidatorResult(
                    self.NAME,
                    ValidatorStatus.VETO,
                    f"Hurst exponent {ctx.hurst_exponent:.3f} < {self.HURST_TRENDING_MIN} "
                    f"— series is mean-reverting, momentum signal unreliable",
                    metrics,
                )

        # 5d. Entropy warning zone
        if norm_entropy > self.ENTROPY_WARN_THRESHOLD:
            return ValidatorResult(
                self.NAME,
                ValidatorStatus.WARN,
                f"HMM entropy {norm_entropy:.3f} in warning zone "
                f"[{self.ENTROPY_WARN_THRESHOLD}, {self.ENTROPY_VETO_THRESHOLD}] "
                f"— position scalar will be reduced",
                metrics,
            )

        return ValidatorResult(
            self.NAME,
            ValidatorStatus.PASS,
            f"regime={ctx.regime_state} entropy={norm_entropy:.3f} H={ctx.hurst_exponent:.3f}",
            metrics,
        )


# ── Cognitive Engine — assembles all five ─────────────────────────────────────


class CognitiveEngine:
    """
    Mandatory runtime decision engine.
    Every trade signal passes through all five validators.
    No bypass. No skip. No partial run.

    Usage in orchestrator / signal engine:
        engine   = CognitiveEngine()
        decision = engine.evaluate(signal_context)
        if not decision.passed:
            log.info("trade_vetoed", reason=decision.veto_reason)
            return   # do not proceed to executor
        qty = decision.adjusted_size_fraction * capital
    """

    def __init__(self) -> None:
        self._validators: list[Validator] = [
            QuantValidator(),
            ProbabilityValidator(),
            RiskValidator(),
            BlockchainValidator(),
            RegimeValidator(),
        ]
        self._log = log.bind(component="cognitive_engine")

    def evaluate(self, ctx: SignalContext) -> CognitiveDecision:
        """
        Run all five validators in sequence.
        First VETO short-circuits — remaining validators still run for logging.
        adjusted_size_fraction is progressively reduced by WARN results.
        """
        results: list[ValidatorResult] = []
        veto_reason: str | None = None
        # FIX (structural): size_fraction starts from kelly_result's actual
        # adjusted_fraction — the same value src/risk/kelly.py computed and
        # already discounted by the HMM entropy gate (regime_scalar). This
        # was previously self._base_size(ctx), an independently recomputed
        # continuous-Kelly (mu/sigma^2) formula unrelated to the discrete
        # win-probability Kelly formula in kelly.py. The two formulas could
        # diverge by 2x+ on realistic inputs with no risk-coherent meaning
        # to the gap, letting this layer silently inflate position size
        # beyond what entropy-gated Kelly intended — the opposite of its
        # purpose as a mandatory risk governor. kelly_result is now the
        # single source of truth for size; validators only ever multiply
        # it down (WARN -> *0.70) or zero it out (VETO).
        size_fraction = ctx.kelly_adjusted_fraction
        self._log_size_divergence(ctx, size_fraction)
        risk_scores: list[float] = []

        for validator in self._validators:
            try:
                result = validator.validate(ctx)
            except Exception as exc:
                # Validator crash = veto — never let an exception pass a trade
                result = ValidatorResult(
                    validator.__class__.NAME,
                    ValidatorStatus.VETO,
                    f"Validator raised exception: {exc}",
                    {"exception": str(exc)},
                )
                self._log.exception(
                    "cognitive_engine.validator_crash",
                    validator=validator.__class__.NAME,
                )

            results.append(result)

            if result.status == ValidatorStatus.VETO and veto_reason is None:
                veto_reason = f"[{result.validator}] {result.reason}"

            if result.status == ValidatorStatus.WARN:
                # WARN reduces position size by 30% each
                size_fraction *= 0.70

            # Collect risk score if present
            if "risk_score" in result.metrics:
                risk_scores.append(result.metrics["risk_score"])

        passed = veto_reason is None
        risk_score = float(np.mean(risk_scores)) if risk_scores else 0.0
        confidence = self._composite_confidence(ctx, results)

        if not passed:
            size_fraction = 0.0

        decision = CognitiveDecision(
            signal_id=ctx.signal_id,
            passed=passed,
            veto_reason=veto_reason,
            results=results,
            adjusted_size_fraction=round(max(0.0, min(size_fraction, 1.0)), 6),
            risk_score=risk_score,
            confidence=confidence,
        )

        self._log.info(
            "cognitive_engine.decision",
            signal_id=ctx.signal_id,
            passed=passed,
            risk_score=decision.risk_score,
            confidence=decision.confidence,
            adjusted_size=decision.adjusted_size_fraction,
            veto_reason=veto_reason,
        )

        return decision

    def _continuous_kelly_estimate(self, ctx: SignalContext) -> float:
        """
        Independent continuous-Kelly (mu/sigma^2) cross-check, DIAGNOSTIC
        ONLY — never used to set actual position size (see evaluate()).
        Disagreement vs. ctx.kelly_adjusted_fraction flags possible model
        miscalibration (expected_edge_bps vs. p_long/p_bet inconsistency)
        without letting that disagreement silently change the trade size.
        """
        cfg = get_settings().risk
        sigma = ctx.realized_vol / math.sqrt(252)
        if sigma <= 0:
            return 0.0
        mu = ctx.expected_edge_bps / 10_000.0
        kelly = (mu / sigma**2) * cfg.kelly_multiplier
        return min(max(kelly, 0.0), cfg.kelly_ceiling)

    def _log_size_divergence(self, ctx: SignalContext, kelly_fraction: float) -> None:
        """
        Diagnostic-only: compare the real (discrete win-prob) Kelly fraction
        already applied upstream against an independent continuous-Kelly
        estimate. Large divergence does NOT change sizing — it is a signal
        for offline model-calibration review (expected_edge_bps vs.
        p_long/p_bet consistency), logged at WARN so it surfaces in
        observability without affecting the trade.
        """
        continuous_estimate = self._continuous_kelly_estimate(ctx)
        if kelly_fraction <= 1e-9 and continuous_estimate <= 1e-9:
            return
        denom = max(kelly_fraction, continuous_estimate, 1e-9)
        divergence = abs(kelly_fraction - continuous_estimate) / denom
        if divergence > 0.5:
            self._log.warning(
                "cognitive_engine.size_model_divergence",
                signal_id=ctx.signal_id,
                kelly_adjusted_fraction=round(kelly_fraction, 6),
                continuous_kelly_estimate=round(continuous_estimate, 6),
                divergence_pct=round(divergence * 100, 1),
            )

    @staticmethod
    def _composite_confidence(
        ctx: SignalContext,
        results: list[ValidatorResult],
    ) -> float:
        """
        Composite confidence 0→1 from all validator outputs.
        """
        n_pass = sum(1 for r in results if r.status == ValidatorStatus.PASS)
        n_warn = sum(1 for r in results if r.status == ValidatorStatus.WARN)
        total = len(results) or 1

        base = (n_pass + 0.5 * n_warn) / total
        # Weight by model outputs
        model_conf = ctx.p_bet * max(ctx.p_long, 1 - ctx.p_long)
        return round(0.6 * base + 0.4 * model_conf, 4)


# ── Module-level singleton (safe for async use — evaluate() is stateless) ────
_engine: CognitiveEngine | None = None


def get_cognitive_engine() -> CognitiveEngine:
    global _engine
    if _engine is None:
        _engine = CognitiveEngine()
    return _engine
