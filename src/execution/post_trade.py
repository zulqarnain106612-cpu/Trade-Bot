"""
PostTradeAnalytics — fill analytics consolidation for executed orders.

Receives RouteResult objects from SmartOrderRouter and computes:
  - realized_slippage_bps: vs. signal price
  - fee_drag_bps: total fee as bps of trade size
  - fill_ratio: filled_qty / requested_qty
  - venue_pnl_attribution: per-venue P&L contribution
  - execution_quality_score: composite 0-1 score

Persists summaries to DuckDB via DuckDBStore.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import structlog

if TYPE_CHECKING:
    from src.data.duckdb_store import DuckDBStore
    from src.execution.router import RouteResult

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class FillRecord:
    ts: datetime
    symbol: str
    side: str
    horizon_idx: int
    venue: str
    algo: str
    signal_price: float
    avg_fill_price: float
    requested_qty: float
    filled_qty: float
    fee_usd: float
    slippage_bps: float
    fill_ratio: float
    execution_quality_score: float
    pnl_usd: float = 0.0
    error: str | None = None


@dataclass
class VenueStats:
    venue: str
    total_fills: int = 0
    total_qty: float = 0.0
    total_fee_usd: float = 0.0
    avg_slippage_bps: float = 0.0
    avg_fill_ratio: float = 0.0
    _slippage_sum: float = field(default=0.0, repr=False)
    _fill_ratio_sum: float = field(default=0.0, repr=False)

    def update(self, fill: FillRecord) -> None:
        self.total_fills += 1
        self.total_qty += fill.filled_qty
        self.total_fee_usd += fill.fee_usd
        self._slippage_sum += fill.slippage_bps
        self._fill_ratio_sum += fill.fill_ratio
        self.avg_slippage_bps = self._slippage_sum / self.total_fills
        self.avg_fill_ratio = self._fill_ratio_sum / self.total_fills


@dataclass
class AlgoStats:
    """
    Running per-algorithm aggregates.

    Mirrors VenueStats deliberately. algo_breakdown() previously derived
    these by iterating the entire fill history, which is what forced that
    history to be retained forever; keeping running sums makes the breakdown
    exact over all time while the raw records stay bounded.
    """

    algo: str
    count: int = 0
    _fill_ratio_sum: float = field(default=0.0, repr=False)
    _slippage_sum: float = field(default=0.0, repr=False)
    _eq_score_sum: float = field(default=0.0, repr=False)

    def update(self, fill: FillRecord) -> None:
        self.count += 1
        self._fill_ratio_sum += fill.fill_ratio
        self._slippage_sum += fill.slippage_bps
        self._eq_score_sum += fill.execution_quality_score

    def as_dict(self) -> dict[str, float]:
        n = max(self.count, 1)
        return {
            "count": float(self.count),
            "avg_fill_ratio": self._fill_ratio_sum / n,
            "avg_slippage_bps": self._slippage_sum / n,
            "avg_eq_score": self._eq_score_sum / n,
        }


# Raw FillRecords retained for the tail-window consumers (recent_fills,
# execution_quality_trend). One record per fill, forever, was an unbounded
# leak in a process designed to run for months: nothing trimmed it and
# nothing needed more than the tail once the all-time aggregates existed.
_MAX_FILL_HISTORY: int = 5_000


class PostTradeAnalytics:
    """
    Consolidates fill analytics from SmartOrderRouter results.

    Maintains in-memory VenueStats and AlgoStats and optionally persists
    FillRecords to DuckDB for later analysis and model retraining.

    Aggregates are unbounded and exact; raw records are bounded to the last
    _MAX_FILL_HISTORY. DuckDB remains the complete record when a store is
    configured — this class is a live view, not the archive.
    """

    def __init__(self, store: DuckDBStore | None = None) -> None:
        self._store = store
        self._venue_stats: dict[str, VenueStats] = {}
        self._algo_stats: dict[str, AlgoStats] = {}
        self._fill_history: deque[FillRecord] = deque(maxlen=_MAX_FILL_HISTORY)

    def record(
        self,
        result: RouteResult,
        symbol: str,
        side: str,
        horizon_idx: int,
        signal_price: float,
        requested_qty: float,
    ) -> FillRecord:
        """
        Record a fill and compute execution quality metrics.

        Returns a FillRecord with all computed fields.
        """
        fill_ratio = result.filled_qty / max(requested_qty, 1e-9)
        fee_drag_bps = (result.fee_usd / max(result.avg_price * result.filled_qty, 1e-9)) * 10_000

        # Execution quality: 0=worst, 1=best
        slippage_score = max(0.0, 1.0 - result.slippage_bps / 50.0)  # penalize >50bps
        fee_score = max(0.0, 1.0 - fee_drag_bps / 10.0)  # penalize >10bps fee
        fill_score = fill_ratio  # [0,1]
        eq_score = float(np.clip((slippage_score + fee_score + fill_score) / 3.0, 0.0, 1.0))

        fill = FillRecord(
            ts=datetime.now(UTC),
            symbol=symbol,
            side=side,
            horizon_idx=horizon_idx,
            venue=result.venue,
            algo=result.algo,
            signal_price=signal_price,
            avg_fill_price=result.avg_price,
            requested_qty=requested_qty,
            filled_qty=result.filled_qty,
            fee_usd=result.fee_usd,
            slippage_bps=result.slippage_bps,
            fill_ratio=fill_ratio,
            execution_quality_score=eq_score,
            error=result.error,
        )

        # Track venue stats
        if result.venue not in self._venue_stats:
            self._venue_stats[result.venue] = VenueStats(venue=result.venue)
        self._venue_stats[result.venue].update(fill)

        # Per-algo aggregates updated on the way past, so algo_breakdown()
        # stays all-time exact without retaining every record to recompute it.
        if result.algo not in self._algo_stats:
            self._algo_stats[result.algo] = AlgoStats(algo=result.algo)
        self._algo_stats[result.algo].update(fill)

        self._fill_history.append(fill)

        if self._store is not None:
            self._persist(fill)

        log.info(
            "fill_recorded",
            symbol=symbol,
            venue=result.venue,
            algo=result.algo,
            fill_ratio=f"{fill_ratio:.2%}",
            slippage_bps=f"{result.slippage_bps:.1f}",
            eq_score=f"{eq_score:.3f}",
        )

        return fill

    def _persist(self, fill: FillRecord) -> None:
        """Persist fill record to DuckDB feature_log table.

        No-op without a store. The one caller already checks, but an assert
        is stripped by `python -O`, which would leave this dereferencing
        None; guarding here makes the method correct on its own terms
        rather than dependent on its caller remembering.
        """
        store = self._store
        if store is None:
            return
        try:
            store.write_feature_log(
                symbol=fill.symbol,
                features={
                    "fill_ratio": fill.fill_ratio,
                    "slippage_bps": fill.slippage_bps,
                    "fee_usd": fill.fee_usd,
                    "execution_quality_score": fill.execution_quality_score,
                    "horizon_idx": float(fill.horizon_idx),
                    "signal_price": fill.signal_price,
                    "avg_fill_price": fill.avg_fill_price,
                    "pnl_usd": fill.pnl_usd,
                },
            )
        except Exception as exc:
            log.warning("fill_persist_failed", exc=str(exc))

    def venue_summary(self) -> dict[str, VenueStats]:
        """Return a copy of per-venue statistics."""
        return dict(self._venue_stats)

    def recent_fills(self, n: int = 100) -> list[FillRecord]:
        """Return the last n FillRecords, oldest first."""
        return list(self._fill_history)[-n:]

    def execution_quality_trend(self, window: int = 50) -> float:
        """Mean execution quality score over the last `window` fills."""
        recent = list(self._fill_history)[-window:]
        if not recent:
            return 0.0
        return float(np.mean([f.execution_quality_score for f in recent]))

    def algo_breakdown(self) -> dict[str, dict[str, float]]:
        """
        Aggregate metrics grouped by algorithm (IOC/iceberg/TWAP).

        Covers every fill ever recorded, not merely the retained window: the
        numbers come from running AlgoStats rather than from re-reducing the
        history. Deriving them from the records is what previously required
        keeping all of them.
        """
        return {algo: stats.as_dict() for algo, stats in self._algo_stats.items()}
