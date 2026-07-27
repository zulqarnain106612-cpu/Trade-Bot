"""
Capital allocator — v2 completion: portfolio-level capital split across
registry strategies for the multi-strategy engine.

Policy: equal-weight among currently *enabled* strategies (kill-switch
respected), scaled by each strategy's own required_capital_fraction() cap
and renormalized so the book never over-allocates. This is a deliberately
simple, auditable starting policy — v9 (self-optimizing allocation)
replaces it with a learned/optimized allocator; this module is the
baseline it must beat before replacing it in production.

Authority:
  - Carver (2019) Systematic Trading Ch.11 — equal-weight as a robust
    starting allocation before optimizing
"""

from __future__ import annotations

from dataclasses import dataclass

from src.strategies.registry import StrategyProtocol


@dataclass(frozen=True, slots=True)
class AllocationResult:
    """Capital fraction assigned to each strategy_id, summing to <= 1.0."""

    fractions: dict[str, float]

    def total(self) -> float:
        return sum(self.fractions.values())


def equal_weight_allocate(
    strategies: tuple[StrategyProtocol, ...],
    enabled_ids: set[str],
) -> AllocationResult:
    """
    Split capital equally among enabled strategies, capped per-strategy by
    required_capital_fraction() and renormalized to sum to at most 1.0.

    Strategies not in enabled_ids (e.g. kill-switched) receive 0.0.
    """
    active = [s for s in strategies if s.strategy_id in enabled_ids]
    if not active:
        return AllocationResult(fractions={s.strategy_id: 0.0 for s in strategies})

    equal_share = 1.0 / len(active)
    capped = {s.strategy_id: min(equal_share, s.required_capital_fraction()) for s in active}
    total_capped = sum(capped.values())

    if total_capped <= 0.0:
        fractions = {s.strategy_id: 0.0 for s in strategies}
    else:
        # Renormalize so the book allocates its full available capital
        # across whichever strategies remain under-cap, without ever
        # exceeding any individual strategy's own ceiling.
        fractions = {
            sid: frac / total_capped * min(total_capped, 1.0) for sid, frac in capped.items()
        }
        for s in strategies:
            fractions.setdefault(s.strategy_id, 0.0)

    return AllocationResult(fractions=fractions)
