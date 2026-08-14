"""
Unified cross-exchange ledger — v3 Multi-Exchange, Multi-Account Execution.

Tracks positions and margin usage across multiple exchange accounts as one
logical book, so risk gates and sizing see a single global picture instead
of per-exchange silos. Pure data structure + accounting logic — actual
order placement stays in src/execution/live.py / paper.py per venue; this
module answers "what is our aggregate exposure right now."

Authority:
  - Chan (2013) Algorithmic Trading Ch.5 — multi-venue book reconciliation
  - Domain Prior: account for fees, slippage, partial fills, latency,
    reconnects — this ledger records venue-attributed positions precisely
    so those costs can be attributed correctly, not netted away.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VenuePosition:
    """One open position on one exchange venue."""

    venue: str
    symbol: str
    quantity: float  # signed: positive = long, negative = short
    entry_price: float
    margin_used_usd: float


class UnifiedLedger:
    """
    Aggregates VenuePosition records across venues into one logical book.

    Usage::

        ledger = UnifiedLedger()
        ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.1, 60000, 600))
        ledger.record_position(VenuePosition("okx", "BTC/USDT", -0.05, 60100, 300))
        ledger.net_exposure("BTC/USDT")  # -> 0.05 (net long)
    """

    def __init__(self) -> None:
        self._positions: dict[tuple[str, str], VenuePosition] = {}

    def record_position(self, position: VenuePosition) -> None:
        """Upsert a venue's position for a symbol (one position per venue+symbol)."""
        self._positions[(position.venue, position.symbol)] = position

    def clear_position(self, venue: str, symbol: str) -> None:
        self._positions.pop((venue, symbol), None)

    def positions_for_symbol(self, symbol: str) -> list[VenuePosition]:
        return [p for p in self._positions.values() if p.symbol == symbol]

    def net_exposure(self, symbol: str) -> float:
        """Net signed quantity across all venues for one symbol."""
        return sum(p.quantity for p in self.positions_for_symbol(symbol))

    def gross_exposure(self, symbol: str) -> float:
        """Sum of absolute quantities across all venues (total notional exposure)."""
        return sum(abs(p.quantity) for p in self.positions_for_symbol(symbol))

    def total_margin_used_usd(self, venue: str | None = None) -> float:
        """Total margin committed, optionally scoped to one venue."""
        positions: Iterable[VenuePosition] = self._positions.values()
        if venue is not None:
            positions = (p for p in positions if p.venue == venue)
        return sum(p.margin_used_usd for p in positions)

    def venues_holding(self, symbol: str) -> list[str]:
        return [p.venue for p in self.positions_for_symbol(symbol)]

    @property
    def all_positions(self) -> list[VenuePosition]:
        return list(self._positions.values())


_ledger: UnifiedLedger = UnifiedLedger()


def get_unified_ledger() -> UnifiedLedger:
    """Module-level singleton for the cross-exchange unified ledger."""
    return _ledger
