"""
Meta-allocator — v9 Self-Optimizing Capital Allocation.

Replaces v2's static equal-weight capital_allocator with a
performance-weighted allocator: capital shifts toward strategies with
better realized risk-adjusted returns (Sharpe), rate-limited so allocation
never swings by more than max_shift_per_step in a single rebalance — this
prevents the allocator itself from becoming a source of instability
(Domain Prior: Kelly is a ceiling, not a target; the same "no runaway
automation" discipline applies to the allocator).

Authority:
  - Grinold & Kahn (1999) "Active Portfolio Management" — Sharpe-weighted
    allocation across independent return streams
  - Domain Prior: this is a genuine architecture upgrade over v2's
    equal-weight baseline, not a replacement for its safety rails
    (per-strategy correlation/kill-switch gating still applies upstream)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StrategyPerformance:
    strategy_id: str
    realized_sharpe: float
    enabled: bool


def _softmax_weights(sharpes: list[float], temperature: float) -> list[float]:
    if not sharpes:
        return []
    scaled = [s / temperature for s in sharpes]
    m = max(scaled)
    exps = [pow(2.718281828459045, s - m) for s in scaled]
    total = sum(exps)
    return [e / total for e in exps] if total > 0 else [1.0 / len(sharpes)] * len(sharpes)


def compute_target_allocation(
    performances: list[StrategyPerformance], temperature: float = 1.0
) -> dict[str, float]:
    """
    Softmax over realized Sharpe among enabled strategies — better Sharpe
    gets proportionally more capital, but no strategy (however good) can
    reach exactly 1.0 or 0.0 the way a hard top-N selection would, keeping
    the book diversified. Disabled strategies always get 0.0.
    """
    if not performances:
        return {}

    active = [p for p in performances if p.enabled]
    if not active:
        return {p.strategy_id: 0.0 for p in performances}

    weights = _softmax_weights([p.realized_sharpe for p in active], temperature)
    allocation = {p.strategy_id: w for p, w in zip(active, weights, strict=True)}
    for p in performances:
        allocation.setdefault(p.strategy_id, 0.0)
    return allocation


def rate_limit_allocation_shift(
    current: dict[str, float], target: dict[str, float], max_shift_per_step: float = 0.10
) -> dict[str, float]:
    """
    Moves each strategy's allocation toward target by at most
    max_shift_per_step per call — prevents the allocator from
    instantaneously reallocating the whole book on a single noisy Sharpe
    estimate.
    """
    if not 0.0 < max_shift_per_step <= 1.0:
        raise ValueError(f"max_shift_per_step must be in (0, 1], got {max_shift_per_step}")

    all_ids = set(current) | set(target)
    result: dict[str, float] = {}
    for sid in all_ids:
        cur = current.get(sid, 0.0)
        tgt = target.get(sid, 0.0)
        delta = tgt - cur
        step = max(-max_shift_per_step, min(max_shift_per_step, delta))
        result[sid] = cur + step
    return result
