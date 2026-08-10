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


class PostTradeAnalytics:
    """
    Consolidates fill analytics from SmartOrderRouter results.

    Maintains in-memory VenueStats and optionally persists FillRecords
    to DuckDB for later analysis and model retraining.
    """

    def __init__(self, store: DuckDBStore | None = None) -> None:
        self._store = store
        self._venue_stats: dict[str, VenueStats] = {}
        self._fill_history: list[FillRecord] = []

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
        """Persist fill record to DuckDB feature_log table."""
        assert self._store is not None
        try:
            self._store.write_feature_log(
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
        """Return the last n FillRecords."""
        return self._fill_history[-n:]

    def execution_quality_trend(self, window: int = 50) -> float:
        """Mean execution quality score over the last `window` fills."""
        recent = self._fill_history[-window:]
        if not recent:
            return 0.0
        return float(np.mean([f.execution_quality_score for f in recent]))

    def algo_breakdown(self) -> dict[str, dict[str, float]]:
        """Aggregate metrics grouped by algorithm (IOC/iceberg/TWAP)."""
        grouped: dict[str, list[FillRecord]] = {}
        for fill in self._fill_history:
            grouped.setdefault(fill.algo, []).append(fill)
        return {
            algo: {
                "count": float(len(fills)),
                "avg_fill_ratio": float(np.mean([f.fill_ratio for f in fills])),
                "avg_slippage_bps": float(np.mean([f.slippage_bps for f in fills])),
                "avg_eq_score": float(np.mean([f.execution_quality_score for f in fills])),
            }
            for algo, fills in grouped.items()
        }
