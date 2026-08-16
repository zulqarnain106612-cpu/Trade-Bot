"""
Disaster recovery reconciliation — v8 Institutional-Grade Operations.

On restart after a crash, the process's last-known in-memory state may
not match the durable record (an order may have filled, partially filled,
or been cancelled while the process was down). This module compares a
local snapshot against a *reference* snapshot and produces a
reconciliation plan — it never applies the plan itself, so a human or an
explicit caller decides how to resolve discrepancies.

The reference is whatever source is authoritative for the caller:
GET /debug/reconcile uses the persisted open-trade table, which is what
survives a crash; an exchange-reported position list is the stronger
reference once a venue query is available. The comparison is the same
either way, which is why the vocabulary here is deliberately neutral.

Authority:
  - Domain Prior: account for partial fills, latency, reconnects — this is
    the recovery-path complement to that execution discipline
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DiscrepancyType(Enum):
    MISSING_LOCALLY = "missing_locally"  # reference has a position we don't know about
    MISSING_IN_REFERENCE = "missing_in_reference"  # we hold a position the reference does not
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
    reference_quantity: float


def reconcile(
    local_snapshot: list[PositionSnapshot],
    reference_snapshot: list[PositionSnapshot],
    quantity_tolerance: float = 1e-8,
) -> list[Discrepancy]:
    """
    Pure comparison — never mutates either snapshot or issues orders.
    Returns every discrepancy found; an empty list means state is
    consistent and no recovery action is needed.
    """
    local_by_symbol = {p.symbol: p.quantity for p in local_snapshot}
    reference_by_symbol = {p.symbol: p.quantity for p in reference_snapshot}
    all_symbols = set(local_by_symbol) | set(reference_by_symbol)

    discrepancies: list[Discrepancy] = []
    for symbol in sorted(all_symbols):
        local_qty = local_by_symbol.get(symbol, 0.0)
        reference_qty = reference_by_symbol.get(symbol, 0.0)

        if abs(local_qty - reference_qty) <= quantity_tolerance:
            continue

        if symbol not in local_by_symbol:
            dtype = DiscrepancyType.MISSING_LOCALLY
        elif symbol not in reference_by_symbol:
            dtype = DiscrepancyType.MISSING_IN_REFERENCE
        else:
            dtype = DiscrepancyType.QUANTITY_MISMATCH

        discrepancies.append(
            Discrepancy(
                symbol=symbol,
                discrepancy_type=dtype,
                local_quantity=local_qty,
                reference_quantity=reference_qty,
            )
        )
    return discrepancies


def is_state_consistent(discrepancies: list[Discrepancy]) -> bool:
    return len(discrepancies) == 0
