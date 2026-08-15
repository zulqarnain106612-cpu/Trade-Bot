"""
Periodic red-team scheduler — v10 Fully Autonomous Multi-Decade Operation.

Tracks when the next full-system stress replay (v9's stress_simulator run
against the then-current live allocation, not a one-time historical
check) is due, on a recurring cadence — a standing operational ritual for
a system meant to run across years, not a single pre-launch audit.

This module is a pure scheduling primitive: it decides *when* a red-team
run is due; it does not execute the stress simulator itself (that stays
in stress_simulator.py, invoked by whatever runtime scheduler — e.g. the
existing src/tuning/scheduler.py pattern — calls this module's is_due()).

Authority:
  - Domain Prior: reproducibility, stability — a red-team cadence must be
    deterministic and auditable, not "whenever someone remembers to run it"
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RedTeamRunRecord:
    ran_at_ms: int
    breached_floor: bool


class RedTeamScheduler:
    """
    Tracks the last red-team run and whether the next one is due, given a
    fixed interval (default: annual, per the roadmap's "periodic (e.g.
    annual)" cadence). Explicit record_run() call after each execution —
    this scheduler never runs anything itself.
    """

    def __init__(self, interval_ms: int = 365 * 24 * 60 * 60 * 1000) -> None:
        if interval_ms <= 0:
            raise ValueError(f"interval_ms must be positive, got {interval_ms}")
        self._interval_ms = interval_ms
        self._last_run: RedTeamRunRecord | None = None

    def is_due(self, now_ms: int) -> bool:
        if self._last_run is None:
            return True
        return (now_ms - self._last_run.ran_at_ms) >= self._interval_ms

    def record_run(self, ran_at_ms: int, breached_floor: bool) -> None:
        self._last_run = RedTeamRunRecord(ran_at_ms=ran_at_ms, breached_floor=breached_floor)

    @property
    def last_run(self) -> RedTeamRunRecord | None:
        return self._last_run

    def ms_until_due(self, now_ms: int) -> int:
        """Non-negative ms remaining until the next run is due (0 if already due)."""
        if self._last_run is None:
            return 0
        remaining = self._interval_ms - (now_ms - self._last_run.ran_at_ms)
        return max(0, remaining)
