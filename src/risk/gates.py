"""
Risk gate engine — hard limits that block new positions.

Gates (all must pass for a trade to proceed):
  0. Slippage / negative-EV veto : expected edge must cover estimated
                               spread + market-impact cost (GAP-001)
  1. Daily drawdown halt     : daily PnL < -2% of starting equity → halt
  2. Consecutive loss halt   : 3+ consecutive losses → halt
  3. Regime gate             : no new positions when regime = volatile
  4. Max position size       : position notional ≤ 5% of capital
  5. Live gate               : both models must pass OOS thresholds before
                               live trading is permitted

Gates are evaluated in order; first failure short-circuits the rest.
All thresholds read from RiskSettings — never hard-coded here.

Authority:
  - López de Prado (2018) AFML Ch.3 (stop-loss barriers as risk gates)
  - Chan (2013) Algorithmic Trading Ch.7 (drawdown controls)
  - Almgren & Chriss (2001) "Optimal Execution of Portfolio Transactions"
    (gate 0 — slippage/market-impact veto, see src/risk/slippage.py)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import structlog

from src.config import (
    REGIME_VOLATILE,
    RiskSettings,
    TradingMode,
    get_settings,
)
from src.risk.slippage import SlippageEstimate


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Gate result types
# ---------------------------------------------------------------------------


class GateStatus(str, Enum):
    """Outcome of a risk gate evaluation."""

    PASS = "pass"
    HALT_NEGATIVE_EV = "halt_negative_ev"
    HALT_DRAWDOWN = "halt_drawdown"
    HALT_CONSECUTIVE_LOSSES = "halt_consecutive_losses"
    HALT_REGIME = "halt_regime"
    HALT_POSITION_SIZE = "halt_position_size"
    HALT_LIVE_GATE = "halt_live_gate"
    HALT_PAPER_ONLY = "halt_paper_only"
    HALT_DRIFT = "halt_drift"  # Performance drift detection (GAP-003)


@dataclass(frozen=True)
class GateResult:
    """
    Outcome of evaluating the full risk gate stack.

    status    : first gate that fired (or PASS)
    passed    : True only when status == GateStatus.PASS
    reason    : human-readable explanation
    details   : structured context for logging / API
    """

    status: GateStatus
    passed: bool
    reason: str
    details: dict[str, object]

    @classmethod
    def pass_gate(cls, details: dict[str, object] | None = None) -> "GateResult":
        return cls(
            status=GateStatus.PASS,
            passed=True,
            reason="all gates passed",
            details=details or {},
        )

    @classmethod
    def fail(
        cls,
        status: GateStatus,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> "GateResult":
        return cls(
            status=status,
            passed=False,
            reason=reason,
            details=details or {},
        )


# ---------------------------------------------------------------------------
# Individual gate functions — pure, testable
# ---------------------------------------------------------------------------


def check_slippage_veto(
    expected_edge_bps: float,
    slippage: SlippageEstimate | None,
    cfg: RiskSettings | None = None,
) -> GateResult:
    """
    Gate 0: pre-trade transaction-cost veto (GAP-001, Almgren-Chriss).

    Rejects a trade whose expected gross edge would be consumed (or
    exceeded) by estimated spread + market-impact cost plus a configured
    safety margin. Evaluated first since a cost-negative trade should
    never reach the other gates — sizing/regime/drawdown checks are
    irrelevant if the trade has no real edge once execution cost is
    included.

    Parameters
    ----------
    expected_edge_bps : signal's expected gross edge in bps, computed
                         upstream by the signal engine from p_long /
                         meta-label probability and modelled win/loss ratio.
    slippage           : SlippageEstimate from SlippageModel.estimate(), or
                         None when no estimate is available yet. This gate
                         fails OPEN (passes) when slippage is None rather
                         than blocking — it is an additive safety check;
                         callers that have not wired a SlippageModel must
                         not be silently blocked. Once every call site
                         supplies an estimate this parameter should stop
                         being optional.
    cfg                : RiskSettings; loaded from global config if None.

    Returns
    -------
    GateResult — PASS or HALT_NEGATIVE_EV.
    """
    if cfg is None:
        cfg = get_settings().risk

    if slippage is None:
        return GateResult.pass_gate(details={"slippage_gate": "skipped_no_estimate"})

    margin = cfg.slippage_veto_margin_bps
    net_edge_bps = expected_edge_bps - slippage.total_slippage_bps - margin

    if net_edge_bps <= 0.0:
        return GateResult.fail(
            GateStatus.HALT_NEGATIVE_EV,
            reason=(
                f"Expected edge {expected_edge_bps:.2f} bps does not cover "
                f"slippage {slippage.total_slippage_bps:.2f} bps + margin "
                f"{margin:.2f} bps — net EV {net_edge_bps:.2f} bps <= 0"
            ),
            details={
                "expected_edge_bps": round(expected_edge_bps, 4),
                "total_slippage_bps": round(slippage.total_slippage_bps, 4),
                "margin_bps": margin,
                "net_edge_bps": round(net_edge_bps, 4),
                "symbol": slippage.symbol,
                "participation_rate": round(slippage.participation_rate, 6),
            },
        )

    return GateResult.pass_gate(
        details={
            "expected_edge_bps": round(expected_edge_bps, 4),
            "total_slippage_bps": round(slippage.total_slippage_bps, 4),
            "net_edge_bps": round(net_edge_bps, 4),
            "symbol": slippage.symbol,
        }
    )


def check_daily_drawdown(
    daily_pnl_usd: float,
    starting_equity_usd: float,
    cfg: RiskSettings | None = None,
) -> GateResult:
    """
    Gate 1: daily drawdown halt.

    Blocks new positions when today's realized PnL has fallen below
    -N% of starting equity (spec: N=2%).

    Parameters
    ----------
    daily_pnl_usd      : today's net realized PnL in USD (negative = loss)
    starting_equity_usd: equity at the start of the trading day

    Returns
    -------
    GateResult — PASS or HALT_DRAWDOWN.
    """
    if cfg is None:
        cfg = get_settings().risk

    if starting_equity_usd <= 0.0:
        return GateResult.fail(
            GateStatus.HALT_DRAWDOWN,
            reason="starting_equity_usd is zero or negative",
            details={"starting_equity_usd": starting_equity_usd},
        )

    drawdown_pct = (daily_pnl_usd / starting_equity_usd) * 100.0
    threshold = -cfg.daily_drawdown_halt_pct

    if drawdown_pct <= threshold:
        return GateResult.fail(
            GateStatus.HALT_DRAWDOWN,
            reason=(
                f"Daily drawdown {drawdown_pct:.2f}% ≤ threshold {threshold:.2f}% "
                f"— trading halted for remainder of session"
            ),
            details={
                "daily_pnl_usd": round(daily_pnl_usd, 2),
                "starting_equity_usd": round(starting_equity_usd, 2),
                "drawdown_pct": round(drawdown_pct, 4),
                "threshold_pct": threshold,
            },
        )

    return GateResult.pass_gate(
        details={
            "daily_pnl_usd": round(daily_pnl_usd, 2),
            "drawdown_pct": round(drawdown_pct, 4),
            "threshold_pct": threshold,
        }
    )


def check_consecutive_losses(
    consecutive_loss_count: int,
    cfg: RiskSettings | None = None,
) -> GateResult:
    """
    Gate 2: consecutive loss halt.

    Blocks new positions after N consecutive losing trades (spec: N=3).
    Resets when a winning trade occurs (tracked externally by executor).

    Parameters
    ----------
    consecutive_loss_count : number of trailing consecutive losing trades

    Returns
    -------
    GateResult — PASS or HALT_CONSECUTIVE_LOSSES.
    """
    if cfg is None:
        cfg = get_settings().risk

    if consecutive_loss_count >= cfg.consecutive_loss_halt:
        return GateResult.fail(
            GateStatus.HALT_CONSECUTIVE_LOSSES,
            reason=(
                f"{consecutive_loss_count} consecutive losses ≥ "
                f"halt threshold {cfg.consecutive_loss_halt}"
            ),
            details={
                "consecutive_losses": consecutive_loss_count,
                "threshold": cfg.consecutive_loss_halt,
            },
        )

    return GateResult.pass_gate(
        details={
            "consecutive_losses": consecutive_loss_count,
            "threshold": cfg.consecutive_loss_halt,
        }
    )


def check_regime_gate(
    regime_state: int,
) -> GateResult:
    """
    Gate 3: regime volatility gate.

    Blocks all new positions when the HMM regime detector returns
    state = REGIME_VOLATILE (state index 2).

    Parameters
    ----------
    regime_state : current canonical regime index (0=ranging, 1=trending, 2=volatile)

    Returns
    -------
    GateResult — PASS or HALT_REGIME.
    """
    if regime_state == REGIME_VOLATILE:
        return GateResult.fail(
            GateStatus.HALT_REGIME,
            reason="Regime is VOLATILE — no new positions permitted",
            details={"regime_state": regime_state, "volatile_index": REGIME_VOLATILE},
        )

    regime_names: dict[int, str] = {0: "ranging", 1: "trending", 2: "volatile"}
    return GateResult.pass_gate(
        details={
            "regime_state": regime_state,
            "regime_name": regime_names.get(regime_state, "unknown"),
        }
    )


def check_position_size(
    notional_usd: float,
    capital_usd: float,
    cfg: RiskSettings | None = None,
) -> GateResult:
    """
    Gate 4: maximum position size.

    Rejects the trade if the requested notional exceeds
    max_position_size_pct of current capital (spec: 5%).

    Parameters
    ----------
    notional_usd : proposed trade notional in USD
    capital_usd  : current equity in USD

    Returns
    -------
    GateResult — PASS or HALT_POSITION_SIZE.
    """
    if cfg is None:
        cfg = get_settings().risk

    if capital_usd <= 0.0:
        return GateResult.fail(
            GateStatus.HALT_POSITION_SIZE,
            reason="capital_usd is zero or negative",
            details={"capital_usd": capital_usd},
        )

    position_pct = (notional_usd / capital_usd) * 100.0
    if position_pct > cfg.max_position_size_pct:
        return GateResult.fail(
            GateStatus.HALT_POSITION_SIZE,
            reason=(
                f"Position {position_pct:.2f}% of capital exceeds "
                f"max {cfg.max_position_size_pct:.1f}%"
            ),
            details={
                "notional_usd": round(notional_usd, 2),
                "capital_usd": round(capital_usd, 2),
                "position_pct": round(position_pct, 4),
                "max_pct": cfg.max_position_size_pct,
            },
        )

    return GateResult.pass_gate(
        details={
            "notional_usd": round(notional_usd, 2),
            "position_pct": round(position_pct, 4),
            "max_pct": cfg.max_position_size_pct,
        }
    )


def check_live_gate(
    trading_mode: TradingMode,
    direction_gate_pass: bool,
    meta_gate_pass: bool,
) -> GateResult:
    """
    Gate 5: live trading model quality gate.

    Live trading is only permitted when:
      - trading_mode == TradingMode.LIVE
      - Both direction and meta-label models pass OOS thresholds
        (OOS Sharpe > 1.5, max DD < 15%, ≥ 500 trades — stored in DB)

    In paper mode this gate always passes (paper needs no quality guard).

    Parameters
    ----------
    trading_mode       : current TradingMode
    direction_gate_pass: latest direction model live_gate_pass from DB
    meta_gate_pass     : latest meta-label model live_gate_pass from DB

    Returns
    -------
    GateResult — PASS or HALT_LIVE_GATE.
    """
    if trading_mode == TradingMode.PAPER:
        return GateResult.pass_gate(
            details={"trading_mode": trading_mode.value, "gate_check": "skipped_paper"}
        )

    if not direction_gate_pass or not meta_gate_pass:
        return GateResult.fail(
            GateStatus.HALT_LIVE_GATE,
            reason=(
                "Live gate blocked: models have not passed OOS validation thresholds. "
                "Run trainer and verify OOS Sharpe > 1.5, max DD < 15%, trades ≥ 500."
            ),
            details={
                "direction_gate_pass": direction_gate_pass,
                "meta_gate_pass": meta_gate_pass,
                "trading_mode": trading_mode.value,
            },
        )

    return GateResult.pass_gate(
        details={
            "direction_gate_pass": direction_gate_pass,
            "meta_gate_pass": meta_gate_pass,
            "trading_mode": trading_mode.value,
        }
    )


def check_paper_minimum_days(
    paper_trading_days: int,
    cfg: RiskSettings | None = None,
    settings_override: int | None = None,
) -> GateResult:
    """
    Guard: minimum paper trading period before live is permitted.

    Spec: 30 days minimum paper trading before any live.

    Parameters
    ----------
    paper_trading_days : number of calendar days paper trading has run
    settings_override  : override min days (from Settings.paper_trading_days_minimum)

    Returns
    -------
    GateResult — PASS or HALT_PAPER_ONLY.
    """
    min_days = settings_override or get_settings().paper_trading_days_minimum

    if paper_trading_days < min_days:
        return GateResult.fail(
            GateStatus.HALT_PAPER_ONLY,
            reason=(
                f"Only {paper_trading_days} days of paper trading completed. "
                f"Minimum {min_days} days required before live trading."
            ),
            details={
                "paper_trading_days": paper_trading_days,
                "minimum_days": min_days,
                "days_remaining": min_days - paper_trading_days,
            },
        )

    return GateResult.pass_gate(
        details={
            "paper_trading_days": paper_trading_days,
            "minimum_days": min_days,
        }
    )


# ---------------------------------------------------------------------------
# Full gate stack — single call from executor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskGateContext:
    """
    All inputs required to evaluate the complete gate stack.

    Passed as a single object to evaluate_all_gates() to keep
    the executor call site clean.
    """

    daily_pnl_usd: float
    starting_equity_usd: float
    consecutive_loss_count: int
    regime_state: int
    notional_usd: float
    capital_usd: float
    trading_mode: TradingMode
    direction_gate_pass: bool
    meta_gate_pass: bool
    # Days of paper trading completed — used by check_paper_minimum_days gate.
    # Derived from the earliest paper equity_curve record timestamp at call site.
    paper_trading_days: int = 0

    # GAP-001 — gate 0 inputs. expected_edge_bps is the signal engine's
    # modelled gross edge; slippage_estimate is the SlippageModel output
    # for this proposed order. slippage_estimate defaults to None so
    # existing call sites that have not yet wired a SlippageModel are not
    # blocked (check_slippage_veto fails open on None — see its docstring).
    expected_edge_bps: float = 0.0
    slippage_estimate: SlippageEstimate | None = None


def evaluate_all_gates(
    ctx: RiskGateContext,
    cfg: RiskSettings | None = None,
) -> GateResult:
    """
    Evaluate all risk gates in sequence.  Returns on first failure.

    Gate order:
      0. Slippage / negative-EV veto (GAP-001)
      1. Daily drawdown
      2. Consecutive losses
      3. Regime
      4. Position size
      5. Paper minimum days (live mode only)
      6. Live model gate

    Parameters
    ----------
    ctx : RiskGateContext with all required inputs
    cfg : RiskSettings; loaded from global config if None

    Returns
    -------
    GateResult — PASS if all gates pass, else the first failing gate.
    """
    if cfg is None:
        cfg = get_settings().risk

    # NEW-004: explicit sequential calls instead of lambda list.
    # Lambdas over a shared ctx variable create closure-capture risk if the
    # evaluation ever moves to a concurrent context. Explicit calls are also
    # fully visible to static analysis and mypy.
    ordered_results: list[GateResult] = [
        check_slippage_veto(ctx.expected_edge_bps, ctx.slippage_estimate, cfg),
        check_daily_drawdown(ctx.daily_pnl_usd, ctx.starting_equity_usd, cfg),
        check_consecutive_losses(ctx.consecutive_loss_count, cfg),
        check_regime_gate(ctx.regime_state),
        check_position_size(ctx.notional_usd, ctx.capital_usd, cfg),
        (
            check_paper_minimum_days(ctx.paper_trading_days)
            if ctx.trading_mode == TradingMode.LIVE
            else GateResult.pass_gate()
        ),
        check_live_gate(ctx.trading_mode, ctx.direction_gate_pass, ctx.meta_gate_pass),
    ]

    for result in ordered_results:
        if not result.passed:
            log.warning(
                "risk.gate.blocked",
                status=result.status.value,
                reason=result.reason,
                **{k: v for k, v in result.details.items()},
            )
            return result

    log.debug(
        "risk.gate.pass",
        regime=ctx.regime_state,
        consecutive_losses=ctx.consecutive_loss_count,
        drawdown_pct=round((ctx.daily_pnl_usd / ctx.starting_equity_usd) * 100.0, 3)
        if ctx.starting_equity_usd > 0
        else 0.0,
        notional_usd=ctx.notional_usd,
    )
    return GateResult.pass_gate()


# ---------------------------------------------------------------------------
# Drawdown tracker — stateful helper for intraday equity monitoring
# ---------------------------------------------------------------------------


class DrawdownTracker:
    """
    Tracks intraday equity and computes rolling drawdown.

    Used by the orchestrator to compute daily_pnl and peak equity
    without requiring a database round-trip on every bar.
    """

    def __init__(self, starting_equity: float) -> None:
        if starting_equity <= 0.0:
            raise ValueError(f"starting_equity must be > 0, got {starting_equity}")
        self._starting: float = starting_equity
        self._peak: float = starting_equity
        self._current: float = starting_equity
        self._daily_start: float = starting_equity

    def update(self, equity: float) -> None:
        """Update current equity and rolling peak."""
        self._current = equity
        if equity > self._peak:
            self._peak = equity

    def reset_daily(self, current_equity: float) -> None:
        """Call at start of each UTC trading day."""
        self._daily_start = current_equity
        self._current = current_equity

    @property
    def daily_pnl_usd(self) -> float:
        return self._current - self._daily_start

    @property
    def daily_pnl_pct(self) -> float:
        if self._daily_start <= 0.0:
            return 0.0
        return (self.daily_pnl_usd / self._daily_start) * 100.0

    @property
    def drawdown_from_peak_pct(self) -> float:
        """
        Drawdown from all-time high-water mark as a percentage.

        SCAN3-007: This is the all-time peak drawdown used for equity curve
        reporting. The daily drawdown gate uses daily_pnl_usd / daily_start
        (a different denominator). Do not confuse these two metrics.
        Also available as drawdown_from_alltime_peak_pct (preferred alias).
        """
        if self._peak <= 0.0:
            return 0.0
        return ((self._current - self._peak) / self._peak) * 100.0

    @property
    def drawdown_from_alltime_peak_pct(self) -> float:
        """Alias for drawdown_from_peak_pct — preferred name for clarity (SCAN3-007)."""
        return self.drawdown_from_peak_pct

    @property
    def drawdown_from_daily_start_pct(self) -> float:
        """
        Drawdown from the daily start equity as a percentage.

        SCAN3-007: This is the metric used by check_daily_drawdown() gate.
        Equivalent to daily_pnl_pct when daily_pnl is negative.
        """
        if self._daily_start <= 0.0:
            return 0.0
        return ((self._current - self._daily_start) / self._daily_start) * 100.0

    @property
    def starting_equity(self) -> float:
        return self._starting

    @property
    def current_equity(self) -> float:
        return self._current

    @property
    def peak_equity(self) -> float:
        return self._peak

    @property
    def daily_start_equity(self) -> float:
        return self._daily_start


# ---------------------------------------------------------------------------
# Gate 6: Performance drift detector (live only)
# ---------------------------------------------------------------------------


def check_performance_drift(drift_detector: Any) -> GateResult:
    """
    Gate 6: Performance drift detection (live trading mode only).

    Halts new positions if model performance has degraded significantly
    in live trading vs training baseline.

    Checks:
      - Sharpe drop >0.5pp
      - Model accuracy drop >10pp
      - Win rate drop >15pp
      - Max drawdown expansion >10pp

    Parameters
    ----------
    drift_detector : PerformanceDriftDetector
        Live performance monitor with baseline and current metrics

    Returns
    -------
    GateResult — PASS if metrics within thresholds, HALT_DRIFT if degraded.

    Authority: López de Prado AFML Ch.11, Aronson (2006) Ch.9
    """
    if drift_detector is None:
        return GateResult.pass_gate(details={"reason": "drift_detector_not_enabled"})

    drift = drift_detector.check_drift()
    if drift.drifted:
        return GateResult.fail(
            GateStatus.HALT_DRIFT,
            reason=drift.reason,
            details={
                "metric": drift.metric,
                "live_value": round(drift.live_value, 4),
                "baseline_value": round(drift.baseline_value, 4),
                "drift_pp": round(drift.drift_pp, 3),
            },
        )

    # Gate passed — metrics healthy
    metrics = drift_detector.get_live_metrics()
    return GateResult.pass_gate(
        details={
            "total_trades": metrics.get("total_live_trades"),
            "rolling_sharpe": metrics.get("rolling_sharpe"),
            "rolling_winrate": metrics.get("rolling_winrate"),
            "rolling_accuracy": metrics.get("rolling_accuracy"),
            "max_drawdown_pct": metrics.get("max_live_drawdown_pct"),
        }
    )
