"""
Strategy-level kill switch — per-(symbol, timeframe) circuit breaker.

The global DrawdownTracker in gates.py halts ALL trading when portfolio
drawdown exceeds a ceiling. This module is finer-grained: it pauses
individual (symbol, timeframe) strategies that fail their own performance
gates, while letting healthy strategies keep running.

Triggers (any one is sufficient to pause):
  1. Consecutive-loss streak exceeds threshold.
  2. Rolling win rate drops below floor (minimum sample required).
  3. Per-strategy drawdown exceeds strategy-level ceiling.

Cool-down: once paused, a strategy waits for ``cooldown_bars`` ticks
before auto-resuming (manual overrides are also supported).

Operators can force-pause / force-resume a strategy via the kill-switch
API (see pause() / resume()), which the /risk/strategy endpoint calls.

Authority:
  Tulchinsky (2019) Finding Alphas — strategy rotation and retirement.
  Carver (2019) Systematic Trading Ch.12 — strategy health monitoring.
  Chan (2013) Algorithmic Trading Ch.6 — position and strategy limits.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_DEFAULT_MAX_CONSECUTIVE_LOSSES: Final[int] = 5
_DEFAULT_MIN_WIN_RATE: Final[float] = 0.30  # below 30% win rate → pause
_DEFAULT_WIN_RATE_MIN_SAMPLE: Final[int] = 20  # don't gate until N trades
_DEFAULT_STRATEGY_DRAWDOWN_CEILING: Final[float] = 0.15  # 15% peak-to-trough
_DEFAULT_COOLDOWN_BARS: Final[int] = 48  # ~12h at 15-min bars


# ---------------------------------------------------------------------------
# Per-strategy stats
# ---------------------------------------------------------------------------


@dataclass
class StrategyState:
    """Mutable runtime state for one (symbol, timeframe) pair."""

    symbol: str
    timeframe: str

    consecutive_losses: int = 0
    n_trades: int = 0
    n_wins: int = 0
    peak_equity: float = 0.0
    current_equity: float = 0.0

    # Pause state
    is_paused: bool = False
    pause_reason: str = ""
    pause_ts: float = 0.0  # wall-clock seconds when paused
    cooldown_bars_remaining: int = 0
    operator_forced: bool = False  # manual override → never auto-resumes

    @property
    def win_rate(self) -> float:
        return self.n_wins / self.n_trades if self.n_trades > 0 else 0.0

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - self.current_equity) / self.peak_equity

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "is_paused": self.is_paused,
            "pause_reason": self.pause_reason,
            "operator_forced": self.operator_forced,
            "cooldown_bars_remaining": self.cooldown_bars_remaining,
            "consecutive_losses": self.consecutive_losses,
            "n_trades": self.n_trades,
            "win_rate": round(self.win_rate, 4),
            "drawdown_pct": round(self.drawdown_pct, 4),
        }


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


class StrategyKillSwitch:
    """
    Per-(symbol, timeframe) circuit breaker with configurable gates.

    Usage::

        ks = StrategyKillSwitch()
        if not ks.is_active("BTC/USDT", "15m"):
            return  # strategy paused

        # After a trade closes:
        ks.record_trade("BTC/USDT", "15m", pnl_usd=50.0, equity_usd=10050.0)
    """

    def __init__(
        self,
        max_consecutive_losses: int = _DEFAULT_MAX_CONSECUTIVE_LOSSES,
        min_win_rate: float = _DEFAULT_MIN_WIN_RATE,
        win_rate_min_sample: int = _DEFAULT_WIN_RATE_MIN_SAMPLE,
        strategy_drawdown_ceiling: float = _DEFAULT_STRATEGY_DRAWDOWN_CEILING,
        cooldown_bars: int = _DEFAULT_COOLDOWN_BARS,
    ) -> None:
        self._max_consec = max_consecutive_losses
        self._min_wr = min_win_rate
        self._wr_min_n = win_rate_min_sample
        self._dd_ceiling = strategy_drawdown_ceiling
        self._cooldown = cooldown_bars

        self._states: dict[tuple[str, str], StrategyState] = {}

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def is_active(self, symbol: str, timeframe: str) -> bool:
        """
        Return True if the strategy is allowed to trade.

        Also decrements the cooldown counter; when cooldown reaches 0
        the strategy auto-resumes (unless operator_forced=True).
        """
        key = (symbol, timeframe)
        state = self._states.get(key)
        if state is None:
            return True  # unknown strategy: allow by default

        if not state.is_paused:
            return True

        if state.operator_forced:
            return False  # manual pause: never auto-resume

        # Decrement cooldown
        if state.cooldown_bars_remaining > 0:
            state.cooldown_bars_remaining -= 1
            return False

        # Cooldown expired — auto-resume
        log.info(
            "strategy_kill_switch.auto_resumed",
            symbol=symbol,
            timeframe=timeframe,
            original_reason=state.pause_reason,
        )
        state.is_paused = False
        state.pause_reason = ""
        state.cooldown_bars_remaining = 0
        return True

    def status(self, symbol: str, timeframe: str) -> dict:
        """Return state dict for the given strategy (empty if unknown)."""
        key = (symbol, timeframe)
        state = self._states.get(key)
        return state.to_dict() if state is not None else {}

    def all_statuses(self) -> list[dict]:
        return [s.to_dict() for s in self._states.values()]

    # ------------------------------------------------------------------
    # Update on trade close
    # ------------------------------------------------------------------

    def record_trade(
        self,
        symbol: str,
        timeframe: str,
        pnl_usd: float,
        equity_usd: float,
    ) -> None:
        """
        Update per-strategy stats after a trade closes and check all gates.

        Parameters
        ----------
        pnl_usd:
            Closed P&L in USD (negative = loss).
        equity_usd:
            Current strategy equity (cumulative realised P&L from inception).
        """
        state = self._get_or_create(symbol, timeframe)

        state.n_trades += 1
        if pnl_usd > 0:
            state.n_wins += 1
            state.consecutive_losses = 0
        else:
            state.consecutive_losses += 1

        state.current_equity = equity_usd
        if equity_usd > state.peak_equity:
            state.peak_equity = equity_usd

        self._evaluate_gates(state)

    # ------------------------------------------------------------------
    # Manual operator overrides
    # ------------------------------------------------------------------

    def pause(self, symbol: str, timeframe: str, reason: str = "manual") -> None:
        """Force-pause a strategy. Will NOT auto-resume until resume() is called."""
        state = self._get_or_create(symbol, timeframe)
        state.is_paused = True
        state.pause_reason = reason
        state.pause_ts = time.time()
        state.operator_forced = True
        state.cooldown_bars_remaining = 0
        log.warning(
            "strategy_kill_switch.forced_pause",
            symbol=symbol,
            timeframe=timeframe,
            reason=reason,
        )

    def resume(self, symbol: str, timeframe: str) -> None:
        """Lift a force-pause or auto-pause for a strategy."""
        key = (symbol, timeframe)
        state = self._states.get(key)
        if state is None:
            return
        state.is_paused = False
        state.pause_reason = ""
        state.pause_ts = 0.0
        state.operator_forced = False
        state.cooldown_bars_remaining = 0
        state.consecutive_losses = 0  # reset streak on manual resume
        log.info("strategy_kill_switch.resumed", symbol=symbol, timeframe=timeframe)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_or_create(self, symbol: str, timeframe: str) -> StrategyState:
        key = (symbol, timeframe)
        if key not in self._states:
            self._states[key] = StrategyState(symbol=symbol, timeframe=timeframe)
        return self._states[key]

    def _evaluate_gates(self, state: StrategyState) -> None:
        if state.is_paused:
            return  # already paused; don't re-trigger

        reason: str = ""

        if state.consecutive_losses >= self._max_consec:
            reason = (
                f"consecutive_losses={state.consecutive_losses} " f">= threshold={self._max_consec}"
            )
        elif state.n_trades >= self._wr_min_n and state.win_rate < self._min_wr:
            reason = (
                f"win_rate={state.win_rate:.3f} < floor={self._min_wr} " f"(n={state.n_trades})"
            )
        elif state.drawdown_pct > self._dd_ceiling:
            reason = f"strategy_drawdown={state.drawdown_pct:.3f} " f"> ceiling={self._dd_ceiling}"

        if reason:
            state.is_paused = True
            state.pause_reason = reason
            state.pause_ts = time.time()
            state.cooldown_bars_remaining = self._cooldown
            log.warning(
                "strategy_kill_switch.auto_paused",
                symbol=state.symbol,
                timeframe=state.timeframe,
                reason=reason,
                cooldown_bars=self._cooldown,
            )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_kill_switch: StrategyKillSwitch | None = None


def get_kill_switch() -> StrategyKillSwitch:
    global _kill_switch
    if _kill_switch is None:
        _kill_switch = StrategyKillSwitch()
    return _kill_switch
