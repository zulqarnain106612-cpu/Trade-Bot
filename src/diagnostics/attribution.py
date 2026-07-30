"""
Per-strategy P&L attribution — v2 Sub-task 4.

Computes realized P&L, Sharpe, Sortino, Calmar, max drawdown, and hit-rate per
strategy_id from a stream of attributed fills. Pure computation, no I/O — the
caller (orchestrator, once fills are tagged with strategy_id at signal
origination per Sub-task 1's registry) feeds fills in via
AttributionTracker.record().

Authority:
  - Sharpe (1966) "Mutual Fund Performance" — risk-adjusted return ratio
  - Sortino & Price (1994) "Performance Measurement in a Downside Risk Framework"
  - Young (1991) "Calmar Ratio" — return / max-drawdown for fat-tail regimes
  - López de Prado (2018) AFML Ch.14 — backtest statistics / hit-rate
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

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


@dataclass
class AttributionTracker:
    """
    Accumulates attributed fills in memory and computes per-strategy stats
    on demand. Orchestrator feeds fills via record() as trades close;
    the FastAPI layer reads via snapshot() for the /strategies/attribution
    endpoint.
    """

    _fills: list[AttributedFill] = field(default_factory=list)

    def record(self, fill: AttributedFill) -> None:
        self._fills.append(fill)
        log.debug(
            "attribution.recorded",
            strategy_id=fill.strategy_id,
            pnl_usd=fill.pnl_usd,
        )

    def snapshot(self) -> dict[str, StrategyAttribution]:
        strategy_ids = {f.strategy_id for f in self._fills}
        return {sid: compute_attribution(sid, self._fills) for sid in strategy_ids}

    def fill_count(self) -> int:
        return len(self._fills)


_tracker: AttributionTracker = AttributionTracker()


def get_attribution_tracker() -> AttributionTracker:
    """Module-level singleton for the strategy attribution tracker."""
    return _tracker
