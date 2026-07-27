"""
Disaster recovery reconciliation — v8 Institutional-Grade Operations.

On restart after a crash, the process's last-known local state may not
match exchange truth (an order may have filled, partially filled, or been
cancelled while the process was down). This module compares a local
snapshot against an exchange-reported position list and produces a
reconciliation plan — it never applies the plan itself, so a human or an
explicit caller decides how to resolve discrepancies.

Authority:
  - Domain Prior: account for partial fills, latency, reconnects — this is
    the recovery-path complement to that execution discipline
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DiscrepancyType(Enum):
    MISSING_LOCALLY = "missing_locally"  # exchange has a position we don't know about
    MISSING_ON_EXCHANGE = "missing_on_exchange"  # we think we have a position; exchange doesn't
    QUANTITY_MISMATCH = "quantity_mismatch"


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    symbol: str
    quantity: float  # signed


@dataclass(frozen=True, slots=True)
class Discrepancy:
    symbol: str
    discrepancy_type: DiscrepancyType
    local_quantity: float
    exchange_quantity: float


def reconcile(
    local_snapshot: list[PositionSnapshot],
    exchange_snapshot: list[PositionSnapshot],
    quantity_tolerance: float = 1e-8,
) -> list[Discrepancy]:
    """
    Pure comparison — never mutates either snapshot or issues orders.
    Returns every discrepancy found; an empty list means state is
    consistent and no recovery action is needed.
    """
    local_by_symbol = {p.symbol: p.quantity for p in local_snapshot}
    exchange_by_symbol = {p.symbol: p.quantity for p in exchange_snapshot}
    all_symbols = set(local_by_symbol) | set(exchange_by_symbol)

    discrepancies: list[Discrepancy] = []
    for symbol in sorted(all_symbols):
        local_qty = local_by_symbol.get(symbol, 0.0)
        exchange_qty = exchange_by_symbol.get(symbol, 0.0)

        if abs(local_qty - exchange_qty) <= quantity_tolerance:
            continue

        if symbol not in local_by_symbol:
            dtype = DiscrepancyType.MISSING_LOCALLY
        elif symbol not in exchange_by_symbol:
            dtype = DiscrepancyType.MISSING_ON_EXCHANGE
        else:
            dtype = DiscrepancyType.QUANTITY_MISMATCH

        discrepancies.append(
            Discrepancy(
                symbol=symbol,
                discrepancy_type=dtype,
                local_quantity=local_qty,
                exchange_quantity=exchange_qty,
            )
        )
    return discrepancies


def is_state_consistent(discrepancies: list[Discrepancy]) -> bool:
    return len(discrepancies) == 0
