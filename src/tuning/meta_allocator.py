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

import threading
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


class AllocationController:
    """
    Holds the allocation the book is actually running and advances it toward
    a freshly computed target one rate-limited step at a time.

    The stateless helpers above are pure, which means whoever calls them has
    to remember the previous allocation for the rate limit to mean anything.
    Nothing did: every caller recomputed a target from scratch, so a single
    noisy Sharpe estimate could reallocate the whole book in one go. This
    class is that memory.

    Stepping is deliberately driven by a fixed rebalance cadence (see
    Orchestrator._allocation_rebalance_loop) rather than by whoever happens
    to read the allocation — otherwise a monitoring dashboard polling the
    API would converge the book at its own poll rate, and an idle dashboard
    would freeze it.
    """

    def __init__(self, max_shift_per_step: float = 0.10) -> None:
        if not 0.0 < max_shift_per_step <= 1.0:
            raise ValueError(f"max_shift_per_step must be in (0, 1], got {max_shift_per_step}")
        self._max_shift = max_shift_per_step
        self._applied: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def max_shift_per_step(self) -> float:
        return self._max_shift

    def applied(self) -> dict[str, float]:
        """The allocation currently in force. Empty before the first step."""
        with self._lock:
            return dict(self._applied)

    def step_toward(self, target: dict[str, float]) -> dict[str, float]:
        """
        Advance the applied allocation one rate-limited step toward `target`
        and return the new applied allocation.

        The first call adopts `target` outright: there is no incumbent
        allocation to protect yet, and creeping up from zero at 10% a step
        would starve the book for ten rebalances after every restart.
        """
        with self._lock:
            if not self._applied:
                self._applied = dict(target)
            else:
                self._applied = rate_limit_allocation_shift(
                    self._applied, target, max_shift_per_step=self._max_shift
                )
            return dict(self._applied)

    def reset(self) -> None:
        """Drop the incumbent allocation so the next step adopts its target."""
        with self._lock:
            self._applied = {}


_controller: AllocationController | None = None
_controller_lock = threading.Lock()


def get_allocation_controller(max_shift_per_step: float | None = None) -> AllocationController:
    """
    Process-wide allocation controller.

    `max_shift_per_step` is only honoured on the first call (the one that
    creates the controller); afterwards the incumbent instance is returned
    unchanged so a later caller cannot silently widen the rate limit that is
    already protecting a live book.
    """
    global _controller
    with _controller_lock:
        if _controller is None:
            _controller = AllocationController(
                max_shift_per_step if max_shift_per_step is not None else 0.10
            )
        return _controller


def reset_allocation_controller() -> None:
    """Test hook — drops the process-wide controller entirely."""
    global _controller
    with _controller_lock:
        _controller = None
