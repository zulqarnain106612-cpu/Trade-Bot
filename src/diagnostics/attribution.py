"""
Performance Attribution — post-trade signal and regime analysis.

Slices closed TradeRecord data along dimensions that matter for
systematic trading diagnostics (López de Prado AFML Ch.14):

  • regime_at_entry  — which HMM state was active
  • timeframe        — scalping / intraday / swing
  • direction        — long vs short
  • p_long bucket    — model confidence quartile

Returns structured attribution dicts suitable for the /debug/attribution
API endpoint and for operator dashboards.

Authority:
  López de Prado (2018) AFML Ch.14 — feature importance and strategy decomposition
  Carver (2019) Systematic Trading Ch.11 — trade attribution
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.data.storage import TradeRecord


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_EPS = 1e-9


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SliceStats:
    """Aggregate statistics for a single attribution slice."""

    n_trades: int = 0
    n_wins: int = 0
    total_pnl_usd: float = 0.0
    total_notional_usd: float = 0.0
    mean_pnl_usd: float = 0.0
    win_rate: float = 0.0
    mean_pnl_pct: float = 0.0
    sharpe: float = 0.0  # annualised, assumes daily bars; 0 if < 2 trades
    expectancy_usd: float = 0.0  # mean_win * win_rate - mean_loss * loss_rate

    # raw pnl list kept for downstream calcs; excluded from API output
    _pnl_list: list[float] = field(default_factory=list, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_trades": self.n_trades,
            "n_wins": self.n_wins,
            "win_rate": round(self.win_rate, 4),
            "total_pnl_usd": round(self.total_pnl_usd, 2),
            "mean_pnl_usd": round(self.mean_pnl_usd, 2),
            "mean_pnl_pct": round(self.mean_pnl_pct, 4),
            "sharpe": round(self.sharpe, 3),
            "expectancy_usd": round(self.expectancy_usd, 2),
        }


@dataclass
class AttributionReport:
    """Full attribution breakdown for a set of closed trades."""

    n_total: int = 0
    n_closed: int = 0  # trades with both entry and exit price
    total_pnl_usd: float = 0.0
    by_regime: dict[str, SliceStats] = field(default_factory=dict)
    by_timeframe: dict[str, SliceStats] = field(default_factory=dict)
    by_direction: dict[str, SliceStats] = field(default_factory=dict)
    by_confidence_quartile: dict[str, SliceStats] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_total": self.n_total,
            "n_closed": self.n_closed,
            "total_pnl_usd": round(self.total_pnl_usd, 2),
            "by_regime": {k: v.to_dict() for k, v in self.by_regime.items()},
            "by_timeframe": {k: v.to_dict() for k, v in self.by_timeframe.items()},
            "by_direction": {k: v.to_dict() for k, v in self.by_direction.items()},
            "by_confidence_quartile": {
                k: v.to_dict() for k, v in self.by_confidence_quartile.items()
            },
        }


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

_REGIME_LABELS: dict[int, str] = {
    0: "ranging",
    1: "trending",
    2: "volatile",
}

_QUARTILE_LABELS = ["Q1_low", "Q2_mid_low", "Q3_mid_high", "Q4_high"]


def build_attribution(trades: list[TradeRecord]) -> AttributionReport:
    """
    Compute attribution report from a list of closed trade records.

    Trades with no exit_price (still open) are counted in n_total
    but excluded from P&L attribution slices.
    """
    report = AttributionReport(n_total=len(trades))

    closed = [t for t in trades if t.exit_price is not None and t.pnl_usd is not None]
    report.n_closed = len(closed)

    if not closed:
        return report

    report.total_pnl_usd = sum(t.pnl_usd for t in closed)  # type: ignore[misc]

    # Slice along each dimension
    report.by_regime = _slice_by(closed, _regime_key)
    report.by_timeframe = _slice_by(closed, lambda t: t.timeframe)
    report.by_direction = _slice_by(closed, lambda t: "long" if t.direction == 1 else "short")
    report.by_confidence_quartile = _confidence_quartiles(closed)

    log.debug(
        "attribution.built",
        n_closed=report.n_closed,
        total_pnl_usd=round(report.total_pnl_usd, 2),
    )
    return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _regime_key(t: TradeRecord) -> str:
    return _REGIME_LABELS.get(t.regime_at_entry, f"regime_{t.regime_at_entry}")


def _slice_by(
    trades: list[TradeRecord],
    key_fn: Any,
) -> dict[str, SliceStats]:
    buckets: dict[str, list[TradeRecord]] = {}
    for t in trades:
        k = key_fn(t)
        buckets.setdefault(k, []).append(t)

    return {k: _compute_stats(v) for k, v in sorted(buckets.items())}


def _confidence_quartiles(trades: list[TradeRecord]) -> dict[str, SliceStats]:
    """
    Assign each trade to a model-confidence quartile based on raw_signal
    (which stores p_long from the XGBoost direction classifier).
    Falls back to meta_label_prob if raw_signal is None.
    """
    scored = [(t.raw_signal if t.raw_signal is not None else t.meta_label_prob, t) for t in trades]
    scored.sort(key=lambda x: x[0])

    n = len(scored)
    buckets: dict[str, list[TradeRecord]] = {lbl: [] for lbl in _QUARTILE_LABELS}

    for i, (_, t) in enumerate(scored):
        q_idx = min(int(i / n * 4), 3)
        buckets[_QUARTILE_LABELS[q_idx]].append(t)

    return {k: _compute_stats(v) for k, v in buckets.items() if v}


def _compute_stats(trades: list[TradeRecord]) -> SliceStats:
    if not trades:
        return SliceStats()

    pnl_list = [t.pnl_usd for t in trades if t.pnl_usd is not None]  # type: ignore[misc]
    pnl_pct_list = [t.pnl_pct for t in trades if t.pnl_pct is not None]  # type: ignore[misc]
    notional_list = [t.notional_usd for t in trades]

    n = len(pnl_list)
    n_wins = sum(1 for p in pnl_list if p > 0)
    total_pnl = sum(pnl_list)
    mean_pnl = total_pnl / n if n else 0.0
    win_rate = n_wins / n if n else 0.0
    mean_pnl_pct = sum(pnl_pct_list) / len(pnl_pct_list) if pnl_pct_list else 0.0
    total_notional = sum(notional_list)

    sharpe = _sharpe(pnl_list)

    wins = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p <= 0]
    mean_win = sum(wins) / len(wins) if wins else 0.0
    mean_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    loss_rate = 1.0 - win_rate
    expectancy = mean_win * win_rate - mean_loss * loss_rate

    return SliceStats(
        n_trades=n,
        n_wins=n_wins,
        total_pnl_usd=total_pnl,
        total_notional_usd=total_notional,
        mean_pnl_usd=mean_pnl,
        win_rate=win_rate,
        mean_pnl_pct=mean_pnl_pct,
        sharpe=sharpe,
        expectancy_usd=expectancy,
        _pnl_list=pnl_list,
    )


def _sharpe(pnl_list: list[float], periods_per_year: int = 252) -> float:
    """
    Annualised Sharpe (0 risk-free rate assumption).

    Treats each trade as a daily observation for annualisation;
    returns 0.0 if < 2 trades.
    """
    if len(pnl_list) < 2:
        return 0.0
    try:
        mean = statistics.mean(pnl_list)
        std = statistics.stdev(pnl_list)
        if std < _EPS:
            return 0.0
        return (mean / std) * math.sqrt(periods_per_year)
    except statistics.StatisticsError:
        return 0.0
