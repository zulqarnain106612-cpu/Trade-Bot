"""
Risk gate engine — hard limits that block new positions.

Gates (all must pass for a trade to proceed):
  0. Capital preservation floor : v10 whole-book peak-drawdown halt, never
                               auto-clears — outermost backstop
  1. Slippage / negative-EV veto : expected edge must cover estimated
                               spread + market-impact cost (GAP-001)
  2. Daily drawdown halt     : daily PnL < -2% of starting equity → halt
  3. Consecutive loss halt   : 3+ consecutive losses → halt
  4. Regime gate             : no new positions when regime = volatile
  5. Max position size       : position notional ≤ 5% of capital
  6. Live gate               : both models must pass OOS thresholds before
                               live trading is permitted

Gates are evaluated in order; first failure short-circuits the rest.
All thresholds read from RiskSettings — never hard-coded here.

Every gate that compares a number against a threshold rejects a non-finite
measurement before comparing it (see _non_finite). IEEE-754 makes every
comparison against NaN False, so an unguarded NaN does not trip a gate — it
passes through it. "Cannot be measured" is treated as "blocked", never as
"within limits"; the one gate whose failure is advisory rather than a halt
(whale activity) reduces size instead. This is distinct from a `None` input,
which means no data was fetched and continues to fail open by design.

Authority:
  - López de Prado (2018) AFML Ch.3 (stop-loss barriers as risk gates)
  - Chan (2013) Algorithmic Trading Ch.7 (drawdown controls)
  - Almgren & Chriss (2001) "Optimal Execution of Portfolio Transactions"
    (gate 0 — slippage/market-impact veto, see src/risk/slippage.py)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

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


class GateStatus(StrEnum):
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
    HALT_EXCHANGE_STRESS = "halt_exchange_stress"  # GAP-015: exchange stress gate
    REDUCE_WHALE_ACTIVITY = "reduce_whale_activity"  # GAP-015: whale selling, size reduced
    HALT_CAPITAL_PRESERVATION = "halt_capital_preservation"  # v10 outermost drawdown floor


def _non_finite(*values: float) -> bool:
    """
    True when any of *values* is NaN or an infinity.

    Every threshold comparison in this module is of the form
    ``measurement <op> limit``, and IEEE-754 makes *every* comparison against
    NaN False. A NaN measurement therefore does not trip the gate — it slips
    past it. `<= 0.0` guards do not help for the same reason, so the check has
    to be explicit and has to come first.

    This is the same defect class already closed one layer down in
    src/risk/kelly.py (VF-024/026/027/028/029/030). The gates are the last
    check before an order, so failing open here is worse: kelly.py can only
    mis-size a trade the gates already approved.
    """
    return any(not math.isfinite(v) for v in values)


@dataclass(frozen=True)
class GateResult:
    """
    Outcome of evaluating the full risk gate stack.

    status      : first gate that fired (or PASS)
    passed      : True only when status == GateStatus.PASS
    reason      : human-readable explanation
    details     : structured context for logging / API
    size_scalar : multiplicative size ceiling in (0, 1] contributed by
                  ADVISORY gates — those that reduce a position rather than
                  veto it. 1.0 means no reduction.

    The scalar exists because this type previously had only pass/fail, which
    left an advisory gate no way to express "proceed, but smaller". The whale
    gate was written as advisory, documented as advisory, and — having no
    channel for a scalar — implemented as a hard fail, so it vetoed trades its
    own design said should proceed at reduced size. See check_whale_activity.
    """

    status: GateStatus
    passed: bool
    reason: str
    details: dict[str, object]
    size_scalar: float = 1.0

    @classmethod
    def pass_gate(cls, details: dict[str, object] | None = None) -> GateResult:
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
    ) -> GateResult:
        return cls(
            status=status,
            passed=False,
            reason=reason,
            details=details or {},
        )

    @classmethod
    def reduce(
        cls,
        status: GateStatus,
        scalar: float,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> GateResult:
        """
        An advisory outcome: the trade proceeds, at `scalar` times the size.

        `passed` is True — an advisory gate is not a veto, and reporting it as
        a failure is exactly the confusion this constructor exists to end.
        The status is preserved so the reason still reaches the audit trail.
        """
        if not 0.0 < scalar <= 1.0:
            raise ValueError(f"advisory scalar must be in (0, 1], got {scalar}")
        return cls(
            status=status,
            passed=True,
            reason=reason,
            details={**(details or {}), "size_scalar": scalar},
            size_scalar=scalar,
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

    # None above means "no estimate was produced" and fails open by design.
    # A non-finite number is the opposite: an estimate was produced and it is
    # garbage. Passing it on would clear the negative-EV veto for a trade
    # whose cost is unknown.
    if _non_finite(expected_edge_bps, slippage.total_slippage_bps):
        return GateResult.fail(
            GateStatus.HALT_NEGATIVE_EV,
            reason=(
                "Non-finite edge or slippage estimate — net EV cannot be "
                "evaluated, so the trade is blocked rather than assumed profitable."
            ),
            details={
                "expected_edge_bps": expected_edge_bps,
                "total_slippage_bps": slippage.total_slippage_bps,
                "symbol": slippage.symbol,
            },
        )

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


def check_capital_preservation_floor(halted: bool) -> GateResult:
    """
    Gate 0b: v10 capital preservation floor — the outermost, whole-book
    drawdown backstop (src/risk/capital_preservation_floor.py).

    Unlike check_daily_drawdown (which resets at UTC midnight), this
    gate reflects a floor that never auto-clears on equity recovery —
    only an explicit, out-of-band re_authorize() call on the
    CapitalPreservationFloor instance can lift it. This gate is a pure
    read of that instance's halted state; it performs no equity math
    itself.

    Parameters
    ----------
    halted : current CapitalPreservationFloor.is_halted value

    Returns
    -------
    GateResult — PASS if not halted, else HALT_CAPITAL_PRESERVATION.
    """
    if halted:
        return GateResult.fail(
            GateStatus.HALT_CAPITAL_PRESERVATION,
            reason=(
                "Capital preservation floor halted: max drawdown from peak "
                "equity breached — requires explicit re_authorize()"
            ),
            details={"halted": True},
        )
    return GateResult.pass_gate(details={"halted": False})


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

    # Checked before the `<= 0.0` guard, which NaN passes.
    if _non_finite(daily_pnl_usd, starting_equity_usd):
        return GateResult.fail(
            GateStatus.HALT_DRAWDOWN,
            reason=(
                "Non-finite daily PnL or starting equity — drawdown cannot be "
                "measured, so trading is halted rather than assumed within limits."
            ),
            details={
                "daily_pnl_usd": daily_pnl_usd,
                "starting_equity_usd": starting_equity_usd,
            },
        )

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

    # Checked before the `<= 0.0` guard, which NaN passes. Without this a NaN
    # notional produces a NaN position_pct, and `nan > max_pct` is False, so
    # the position-size gate approves a position of unknown size.
    if _non_finite(notional_usd, capital_usd):
        return GateResult.fail(
            GateStatus.HALT_POSITION_SIZE,
            reason=(
                "Non-finite notional or capital — position size cannot be "
                "measured against the limit, so the trade is blocked."
            ),
            details={"notional_usd": notional_usd, "capital_usd": capital_usd},
        )

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
    # GAP-003 PerformanceDriftDetector. Typed Any to avoid a gates ->
    # performance_drift import edge; check_performance_drift already takes Any
    # and only calls check_drift()/get_live_metrics() on it. None = no detector
    # supplied = the gate passes, which is how it stays inert until wired.
    drift_detector: Any = None

    # GAP-015 — intelligence gate inputs (gates 9 & 10).
    # Populated from BinanceIntelligenceProvider.fetch_metrics();
    # default None → gates fail open (PASS) so a provider outage
    # never blocks trading.
    exchange_stress_score: float | None = None
    whale_buy_sell_ratio: float | None = None
    # The size multiplier applied when REDUCE_WHALE_ACTIVITY fires *and*
    # RISK_WHALE_GATE_ADVISORY is on. Previously this field claimed to be set
    # by evaluate_all_gates() and returned in details["whale_scalar"] — it was
    # neither: nothing in the repository ever wrote it, read it, or emitted
    # it, so the documented 50% reduction never happened and the gate blocked
    # instead. It is now the gate's input, and the resulting reduction travels
    # out on GateResult.size_scalar.
    whale_scalar: float = 0.5

    # v10 — outermost capital preservation floor state. Defaults to False
    # (not halted) so existing call sites that have not yet wired a
    # CapitalPreservationFloor instance are unaffected.
    capital_preservation_halted: bool = False


def evaluate_all_gates(
    ctx: RiskGateContext,
    cfg: RiskSettings | None = None,
) -> GateResult:
    """
    Evaluate all risk gates in sequence.  Returns on first failure.

    Gate order:
      0. Capital preservation floor (v10 — never auto-clears)
      1. Slippage / negative-EV veto (GAP-001)
      2. Daily drawdown
      3. Consecutive losses
      4. Regime
      5. Position size
      6. Paper minimum days (live mode only)
      7. Live model gate

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
        check_capital_preservation_floor(ctx.capital_preservation_halted),
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
        # GAP-003 performance drift. Live-only, matching the paper-minimum-days
        # gate above and this gate's own contract: halting the paper track on
        # drift would stop the very run that is meant to be gathering the
        # evidence about whether the drift persists.
        #
        # Grouped with check_live_gate because both answer "is the model still
        # good enough to trade", as opposed to the position-level checks above
        # them. Fails open on a None detector, so this is a no-op until the
        # orchestrator actually supplies one.
        (
            check_performance_drift(ctx.drift_detector)
            if ctx.trading_mode == TradingMode.LIVE
            else GateResult.pass_gate()
        ),
        # GAP-015: intelligence gates — fail open (PASS) when data unavailable
        check_exchange_stress(ctx.exchange_stress_score),
        check_whale_activity(
            ctx.whale_buy_sell_ratio,
            advisory=(cfg or get_settings().risk).whale_gate_advisory,
            advisory_scalar=ctx.whale_scalar,
        ),
    ]

    # Advisory results pass, so they do not short-circuit; their scalars
    # multiply into the ceiling carried by the final PASS. Only a genuine
    # veto returns early.
    advisory_scalar = 1.0
    advisory_details: dict[str, object] = {}
    for result in ordered_results:
        if not result.passed:
            log.warning(
                "risk.gate.blocked",
                status=result.status.value,
                reason=result.reason,
                **dict(result.details.items()),
            )
            return result
        if result.size_scalar < 1.0:
            advisory_scalar *= result.size_scalar
            advisory_details[f"{result.status.value}_scalar"] = result.size_scalar
            log.info(
                "risk.gate.reduced",
                status=result.status.value,
                scalar=result.size_scalar,
                reason=result.reason,
            )

    log.debug(
        "risk.gate.pass",
        regime=ctx.regime_state,
        consecutive_losses=ctx.consecutive_loss_count,
        drawdown_pct=round((ctx.daily_pnl_usd / ctx.starting_equity_usd) * 100.0, 3)
        if ctx.starting_equity_usd > 0
        else 0.0,
        notional_usd=ctx.notional_usd,
        size_scalar=round(advisory_scalar, 4),
    )
    if advisory_scalar < 1.0:
        return GateResult.reduce(
            GateStatus.PASS,
            advisory_scalar,
            reason="all gates passed; advisory gates reduced size",
            details=advisory_details,
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

    try:
        drift = drift_detector.check_drift()
    except Exception as exc:
        # Fail CLOSED, unlike the intelligence gates below. Those fail open
        # because a third-party feed being down says nothing about our model;
        # this gate measures our own realized performance, so a check that
        # cannot run means the model's state is unknown -- and opening a new
        # position on an unknown model is the exact hazard the gate exists
        # for. check_drift() already returns drifted=False when it merely
        # lacks data, so reaching here is a genuine fault, not a cold start.
        #
        # Live-only (see evaluate_all_gates), so paper keeps running, and an
        # operator who needs to trade through it can clear the detector.
        log.error("gates.drift_check_failed", error=str(exc), exc_info=True)
        return GateResult.fail(
            GateStatus.HALT_DRIFT,
            reason=f"performance drift check failed: {exc}",
            details={"metric": "unavailable", "error": str(exc)},
        )

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


# ---------------------------------------------------------------------------
# GAP-013 -- automated position-exit evaluation (stop-loss / take-profit /
# time-based exit). Pure function: takes a position snapshot + the current
# ---------------------------------------------------------------------------
# Intelligence gates — GAP-015
# Sourced from BinanceIntelligenceProvider (free public API; no key required).
# ExchangeStressGate: halt when composite stress (basis+funding+OI) exceeds
#   threshold.  Protects against contagion/counterparty risk.
# WhaleActivityGate: taker-sell pressure dominates. Blocks by default; with
#   RISK_WHALE_GATE_ADVISORY=true it instead passes with
#   GateResult.size_scalar < 1.0 so sizing shrinks rather than blocking.
#   Consuming that scalar in Kelly is still pending — see check_whale_activity.
# Both gates fail open (PASS) when intelligence_metrics is None so the
# signal path is never blocked by a provider failure.
# ---------------------------------------------------------------------------


def check_exchange_stress(
    exchange_stress_score: float | None,
    stress_halt_threshold: float = 0.75,
    stress_reduce_threshold: float = 0.50,
) -> GateResult:
    """
    Gate 9: halt or reduce when exchange stress composite is elevated.

    exchange_stress_score in [0, 1] is computed by BinanceIntelligenceProvider
    as a weighted composite of:
      - perp/spot basis divergence (35%)
      - funding rate z-score vs 30-day history (40%)
      - 24h open interest change / rapid deleveraging (25%)

    Parameters
    ----------
    exchange_stress_score    : composite stress [0, 1]; None → fail open (PASS).
    stress_halt_threshold    : score above which trading halts (default 0.75).
    stress_reduce_threshold  : score above which a reduction warning is emitted
                               but trading is NOT blocked (default 0.50).

    Returns
    -------
    GateResult — PASS, HALT_EXCHANGE_STRESS.
    Note: REDUCE path emits a warning log but still returns PASS so as not
    to block the trade; the caller may inspect details["stress_action"] to
    further reduce sizing.
    """
    if exchange_stress_score is None:
        return GateResult.pass_gate(details={"exchange_stress_gate": "skipped_no_data"})

    score = float(exchange_stress_score)

    # None (above) means the provider returned no data and is a deliberate
    # fail-open. NaN means it returned data that is not a number — the stress
    # composite arrived broken. `score > halt_threshold` is False for NaN, so
    # without this the most stressed possible reading passes the halt.
    if _non_finite(score):
        return GateResult.fail(
            GateStatus.HALT_EXCHANGE_STRESS,
            reason=(
                "Non-finite exchange stress score — contagion risk cannot be "
                "assessed, so new positions are halted rather than assumed safe."
            ),
            details={"exchange_stress_score": exchange_stress_score},
        )

    if score > stress_halt_threshold:
        return GateResult.fail(
            GateStatus.HALT_EXCHANGE_STRESS,
            reason=(
                f"Exchange stress score {score:.3f} > halt threshold "
                f"{stress_halt_threshold:.2f}. "
                "Contagion/counterparty risk detected — halting new positions."
            ),
            details={
                "exchange_stress_score": round(score, 4),
                "halt_threshold": stress_halt_threshold,
                "stress_action": "halt",
            },
        )

    if score > stress_reduce_threshold:
        log.warning(
            "risk.gate.exchange_stress_elevated",
            score=round(score, 4),
            reduce_threshold=stress_reduce_threshold,
            action="reduce_suggested",
        )
        return GateResult.pass_gate(
            details={
                "exchange_stress_score": round(score, 4),
                "reduce_threshold": stress_reduce_threshold,
                "stress_action": "reduce_suggested",
            }
        )

    return GateResult.pass_gate(
        details={
            "exchange_stress_score": round(score, 4),
            "stress_action": "none",
        }
    )


def _whale_outcome(
    status: GateStatus,
    advisory: bool,
    scalar: float,
    *,
    reason: str,
    details: dict[str, object],
) -> GateResult:
    """
    Express a whale trigger as either a size reduction or a veto.

    Advisory is what the gate was designed for and what its surrounding
    documentation has always described. It is NOT the default, because for
    the life of this gate the code has vetoed instead, and switching a live
    risk control from "block" to "trade at half size" is a trading-policy
    change an operator makes deliberately — not one that arrives inside a
    bug fix. RISK_WHALE_GATE_ADVISORY=true opts in.
    """
    if advisory:
        return GateResult.reduce(
            status,
            scalar,
            reason=f"{reason} Size reduced to {scalar:.0%} rather than blocked.",
            details={**details, "whale_action": f"reduce_to_{scalar:.0%}"},
        )
    return GateResult.fail(
        status,
        reason=f"{reason} Trade blocked (advisory mode off).",
        details={**details, "whale_action": "block"},
    )


def check_whale_activity(
    whale_buy_sell_ratio: float | None,
    sell_threshold: float = 0.85,
    *,
    advisory: bool = False,
    advisory_scalar: float = 0.5,
) -> GateResult:
    """
    Gate 10: taker-flow whale activity filter (sizing advisory).

    whale_buy_sell_ratio = taker_buy_vol / taker_sell_vol over last 12h,
    computed from Binance Futures klines (public, no key).

    Values:
      > 1.0 : net buying pressure (bullish taker flow)
      = 1.0 : neutral
      < 1.0 : net selling pressure (bearish taker flow)

    This gate returns REDUCE_WHALE_ACTIVITY when ratio < sell_threshold,
    which is NOT a halt — it is advisory.  The orchestrator/signal engine
    should apply a 0.5 position scalar when this status is returned.
    The gate never blocks a trade outright; only extreme exchange stress does.

    Parameters
    ----------
    whale_buy_sell_ratio : taker buy/sell ratio [0, 10]; None → fail open (PASS).
    sell_threshold       : ratio below which selling pressure is significant.

    Returns
    -------
    GateResult — PASS or REDUCE_WHALE_ACTIVITY.
    """
    if whale_buy_sell_ratio is None:
        return GateResult.pass_gate(details={"whale_gate": "skipped_no_data"})

    ratio = float(whale_buy_sell_ratio)

    # As with exchange stress: None is "no data" and fails open, a non-finite
    # number is corrupt data. This gate's failure is advisory (halve the
    # position), so an unreadable taker flow reduces size rather than
    # silently reading as neutral.
    if _non_finite(ratio):
        return _whale_outcome(
            GateStatus.REDUCE_WHALE_ACTIVITY,
            advisory,
            advisory_scalar,
            reason=(
                "Non-finite whale buy/sell ratio — taker flow cannot be read, "
                "so position size is reduced rather than assumed neutral."
            ),
            details={"whale_buy_sell_ratio": whale_buy_sell_ratio},
        )

    if ratio < sell_threshold:
        return _whale_outcome(
            GateStatus.REDUCE_WHALE_ACTIVITY,
            advisory,
            advisory_scalar,
            reason=(
                f"Whale taker sell pressure: buy/sell ratio {ratio:.3f} < "
                f"threshold {sell_threshold:.2f}. Net selling pressure detected."
            ),
            details={
                "whale_buy_sell_ratio": round(ratio, 4),
                "sell_threshold": sell_threshold,
            },
        )

    return GateResult.pass_gate(
        details={
            "whale_buy_sell_ratio": round(ratio, 4),
            "whale_action": "none",
        }
    )


# runtime-toggleable exit-control settings, returns an exit decision or None
# if the position should remain open. No I/O, no side effects -- the caller
# (Orchestrator's position-monitor loop) is responsible for actually calling
# close_position() when this returns a reason.
# ---------------------------------------------------------------------------


def check_position_exit(
    unrealized_pnl_pct: float,
    entry_ts_ms: int,
    now_ts_ms: int,
    stop_loss_enabled: bool,
    stop_loss_pct: float,
    take_profit_enabled: bool,
    take_profit_pct: float,
    max_holding_period_s: float,
    trailing_stop_enabled: bool = False,
    trailing_stop_pct: float = 1.5,
    peak_unrealized_pct: float = 0.0,
) -> str | None:
    """
    Evaluate whether an open position should be closed automatically.

    Checked in order (first match wins): stop-loss, take-profit, time exit.
    Stop-loss is checked first on purpose -- if a position has somehow
    crossed both thresholds between monitor ticks (e.g. a large gap move),
    capping the loss takes priority over capturing the gain.

    Parameters
    ----------
    unrealized_pnl_pct    : current unrealized PnL as a percentage of
                            notional (e.g. -2.5 means -2.5% / a loss),
                            matching the sign convention already used by
                            open_positions_safe()'s "unrealized_pnl_pct" field
                            in both PaperExecutor and LiveExecutor.
    entry_ts_ms           : position entry timestamp, epoch milliseconds
                            (matches PaperPosition.entry_ts / LivePosition.entry_ts).
    now_ts_ms             : current timestamp, epoch milliseconds.
    stop_loss_enabled     : runtime toggle (RuntimeConfig.get_risk_controls()).
    stop_loss_pct         : close when unrealized_pnl_pct <= -stop_loss_pct.
    take_profit_enabled   : runtime toggle.
    take_profit_pct       : close when unrealized_pnl_pct >= take_profit_pct.
    max_holding_period_s  : close when (now - entry) >= this many seconds.
                            Always enforced -- there is no runtime toggle for
                            the time exit, since an unbounded holding period
                            for a position the model no longer has live edge
                            estimates for is a correctness issue, not merely a
                            risk-preference choice the operator should be able
                            to switch off.
    trailing_stop_enabled : when True, close if unrealized_pnl_pct has
                            retreated more than trailing_stop_pct from its
                            all-time peak since entry.
    trailing_stop_pct     : drawdown from peak that triggers the trailing stop.
    peak_unrealized_pct   : highest unrealized PnL % seen since position open
                            (tracked by PaperPosition.mark / LivePosition.mark).

    Returns
    -------
    str | None -- one of "stop_loss", "trailing_stop", "profit_target",
    "time_exit", "invalid_mark", or None if the position should remain open.
    """
    # A non-finite mark disables stop-loss, trailing stop and take-profit in
    # one go — all three are comparisons against unrealized_pnl_pct, and all
    # three are False for NaN. The position would then sit unprotected until
    # the time exit, which is precisely the situation a stop-loss exists for.
    # Closing is the conservative response: a position whose P&L cannot be
    # read cannot be risk-managed, and it gets its own reason string so the
    # trade record does not claim a stop-loss that was never measured.
    if _non_finite(unrealized_pnl_pct):
        return "invalid_mark"

    if stop_loss_enabled and unrealized_pnl_pct <= -abs(stop_loss_pct):
        return "stop_loss"

    if trailing_stop_enabled and peak_unrealized_pct > 0.0:
        drawdown_from_peak = peak_unrealized_pct - unrealized_pnl_pct
        if drawdown_from_peak >= abs(trailing_stop_pct):
            return "trailing_stop"

    if take_profit_enabled and unrealized_pnl_pct >= abs(take_profit_pct):
        return "profit_target"

    holding_period_s = (now_ts_ms - entry_ts_ms) / 1000.0
    if holding_period_s >= max_holding_period_s:
        return "time_exit"

    return None
