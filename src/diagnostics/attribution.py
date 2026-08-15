"""
Per-strategy P&L attribution — v2 Sub-task 4.

Computes realized P&L, Sharpe, Sortino, Calmar, max drawdown, and hit-rate per
strategy_id from a stream of attributed fills. Pure computation, no I/O — the
caller (orchestrator, once fills are tagged with strategy_id at signal
origination per Sub-task 1's registry) feeds fills in via
AttributionTracker.record().

Retention: the tracker keeps a bounded per-strategy window of raw fills
(``MAX_FILLS_PER_STRATEGY``) plus unbounded *scalar* lifetime aggregates.
An unbounded fill list would grow for the life of the process and make
``snapshot()`` — which the capital allocator calls on every allocation —
cost more every day it stays up. Counts and totals that must not shrink
when a fill is evicted (trade_count, total_pnl_usd, win_rate,
first_entry_ts) are accumulated as running scalars; the path-dependent
ratios (Sharpe, Sortino, Calmar, max drawdown) are computed over the
retained window, which also makes them track recent behaviour rather than
being anchored to a strategy's first month forever.

Authority:
  - Sharpe (1966) "Mutual Fund Performance" — risk-adjusted return ratio
  - Sortino & Price (1994) "Performance Measurement in a Downside Risk Framework"
  - Young (1991) "Calmar Ratio" — return / max-drawdown for fat-tail regimes
  - López de Prado (2018) AFML Ch.14 — backtest statistics / hit-rate
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field, replace

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AttributedFill:
    """One realized trade outcome tagged with the strategy that generated it."""

    strategy_id: str
    pnl_usd: float
    entry_ts: int
    exit_ts: int


@dataclass(frozen=True, slots=True)
class StrategyAttribution:
    """Aggregate performance stats for one strategy_id."""

    strategy_id: str
    trade_count: int
    total_pnl_usd: float
    win_rate: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown_usd: float

    def to_dict(self) -> dict[str, float | int | str]:
        return {
            "strategy_id": self.strategy_id,
            "trade_count": self.trade_count,
            "total_pnl_usd": round(self.total_pnl_usd, 4),
            "win_rate": round(self.win_rate, 4),
            "sharpe": round(self.sharpe, 4),
            "sortino": round(self.sortino, 4),
            "calmar": round(self.calmar, 4),
            "max_drawdown_usd": round(self.max_drawdown_usd, 4),
        }


def _sharpe(pnls: list[float]) -> float:
    if len(pnls) < 2:
        return 0.0
    mean = sum(pnls) / len(pnls)
    variance = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
    std = math.sqrt(variance)
    if std == 0.0:
        return 0.0
    return mean / std


def _sortino(pnls: list[float]) -> float:
    """Mean / downside-deviation (semi-deviation of losses only)."""
    if len(pnls) < 2:
        return 0.0
    mean = sum(pnls) / len(pnls)
    losses = [p for p in pnls if p < 0.0]
    if not losses:
        # No losses: Sortino is undefined; return a large positive value
        # floored to the Sharpe ratio so callers see consistent ordering.
        return _sharpe(pnls)
    downside_var = sum(p**2 for p in losses) / len(pnls)
    downside_std = math.sqrt(downside_var)
    if downside_std == 0.0:
        return 0.0
    return mean / downside_std


def _calmar(pnls: list[float], max_dd: float) -> float:
    """Total P&L / max drawdown — penalizes strategies with large peak-to-trough."""
    if max_dd <= 0.0:
        return 0.0
    return sum(pnls) / max_dd


def _max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def compute_attribution(strategy_id: str, fills: list[AttributedFill]) -> StrategyAttribution:
    """Pure computation of one strategy's attribution stats from its fills."""
    own_fills = [f for f in fills if f.strategy_id == strategy_id]
    pnls = [f.pnl_usd for f in own_fills]

    if not pnls:
        return StrategyAttribution(
            strategy_id=strategy_id,
            trade_count=0,
            total_pnl_usd=0.0,
            win_rate=0.0,
            sharpe=0.0,
            sortino=0.0,
            calmar=0.0,
            max_drawdown_usd=0.0,
        )

    wins = sum(1 for p in pnls if p > 0)
    max_dd = _max_drawdown(pnls)
    return StrategyAttribution(
        strategy_id=strategy_id,
        trade_count=len(pnls),
        total_pnl_usd=sum(pnls),
        win_rate=wins / len(pnls),
        sharpe=_sharpe(pnls),
        sortino=_sortino(pnls),
        calmar=_calmar(pnls, max_dd),
        max_drawdown_usd=max_dd,
    )


# Per-strategy retention for the path-dependent ratios. 2000 fills is far
# more than any ratio here needs to be stable, and bounds a long-running
# process at a few hundred KB per strategy.
MAX_FILLS_PER_STRATEGY: int = 2000


@dataclass(slots=True)
class _StrategyState:
    """One strategy's retained fill window plus its lifetime scalars."""

    fills: deque[AttributedFill]
    lifetime_trades: int = 0
    lifetime_pnl_usd: float = 0.0
    lifetime_wins: int = 0
    first_entry_ts: int | None = None


@dataclass
class AttributionTracker:
    """
    Accumulates attributed fills in memory and computes per-strategy stats
    on demand. Orchestrator feeds fills via record() as trades close;
    the FastAPI layer reads via snapshot() for the /strategies/attribution
    endpoint.

    Memory is bounded: see the module docstring for which figures survive
    eviction (counts, totals, first entry) and which are window-scoped
    (Sharpe, Sortino, Calmar, max drawdown).
    """

    max_fills_per_strategy: int = MAX_FILLS_PER_STRATEGY
    _states: dict[str, _StrategyState] = field(default_factory=dict)

    def _state(self, strategy_id: str) -> _StrategyState:
        state = self._states.get(strategy_id)
        if state is None:
            state = _StrategyState(fills=deque(maxlen=self.max_fills_per_strategy))
            self._states[strategy_id] = state
        return state

    def record(self, fill: AttributedFill) -> None:
        state = self._state(fill.strategy_id)
        state.fills.append(fill)
        state.lifetime_trades += 1
        state.lifetime_pnl_usd += fill.pnl_usd
        if fill.pnl_usd > 0:
            state.lifetime_wins += 1
        if state.first_entry_ts is None or fill.entry_ts < state.first_entry_ts:
            state.first_entry_ts = fill.entry_ts
        log.debug(
            "attribution.recorded",
            strategy_id=fill.strategy_id,
            pnl_usd=fill.pnl_usd,
        )

    def snapshot(self) -> dict[str, StrategyAttribution]:
        """
        Per-strategy stats. Counts and totals are lifetime; the risk ratios
        are computed over the retained window.
        """
        out: dict[str, StrategyAttribution] = {}
        for strategy_id, state in self._states.items():
            windowed = compute_attribution(strategy_id, list(state.fills))
            out[strategy_id] = replace(
                windowed,
                trade_count=state.lifetime_trades,
                total_pnl_usd=state.lifetime_pnl_usd,
                win_rate=(
                    state.lifetime_wins / state.lifetime_trades if state.lifetime_trades else 0.0
                ),
            )
        return out

    def fills_for(self, strategy_id: str) -> list[AttributedFill]:
        """
        This strategy's retained fills, in record order — the window, not
        every fill ever recorded.

        snapshot() aggregates away the timestamps; the promotion gauntlet
        (src/tuning/promotion_gauntlet.py) needs them to know how long a
        candidate has actually been running. Pair this with
        first_entry_ts_for(), which does not shrink under eviction.
        """
        state = self._states.get(strategy_id)
        return list(state.fills) if state is not None else []

    def first_entry_ts_for(self, strategy_id: str) -> int | None:
        """
        Earliest entry timestamp ever recorded for this strategy, or None.

        Survives window eviction, so a long-lived strategy does not appear
        to have started trading recently once its oldest fills age out.
        """
        state = self._states.get(strategy_id)
        return state.first_entry_ts if state is not None else None

    def lifetime_trade_count(self, strategy_id: str) -> int:
        """Every fill ever recorded for this strategy, not just the window."""
        state = self._states.get(strategy_id)
        return state.lifetime_trades if state is not None else 0

    def fill_count(self) -> int:
        """Lifetime fills across all strategies."""
        return sum(s.lifetime_trades for s in self._states.values())

    def reset(self) -> None:
        """
        Drop all state, including the lifetime scalars.

        The tracker is a process-wide singleton, so tests need a supported
        way to isolate themselves rather than reaching into its internals.
        Nothing in the trading path calls this — a live reset would erase
        the very history the allocator and the gauntlet size decisions on.
        """
        self._states.clear()


_tracker: AttributionTracker = AttributionTracker()


def get_attribution_tracker() -> AttributionTracker:
    """Module-level singleton for the strategy attribution tracker."""
    return _tracker
