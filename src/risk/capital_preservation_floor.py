"""
Capital preservation floor — v10 Fully Autonomous Multi-Decade Operation.

The final backstop beneath every other automated decision layer (v2's
kill-switch, v4's drift detection, v7's macro budget, v9's meta-allocator):
a hard, code-enforced maximum drawdown that halts ALL trading and requires
explicit human re-authorization to resume. Unlike the per-strategy
kill-switch (v2), this operates at the whole-book level and is designed
to never be bypassed by any automated process — only an explicit,
out-of-band re-authorization call clears it.

Authority:
  - Domain Prior: enforce drawdown and position limits; Kelly is a
    ceiling, not a target — this is the outermost such ceiling, beneath
    which no automated system may trade regardless of any other signal
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReAuthorization:
    """Records who/when cleared the floor — never auto-generated."""

    authorized_by: str
    reason: str
    at_ms: int


class CapitalPreservationFloor:
    """
    Tracks peak equity and halts trading (halted=True) the instant
    drawdown from peak exceeds max_drawdown_pct. Once halted, only
    re_authorize() can clear it — no automated code path may call
    re_authorize() from within this module or any other risk module.
    """

    def __init__(self, max_drawdown_pct: float = 0.30) -> None:
        if not 0.0 < max_drawdown_pct < 1.0:
            raise ValueError(f"max_drawdown_pct must be in (0, 1), got {max_drawdown_pct}")
        self._max_drawdown_pct = max_drawdown_pct
        self._peak_equity: float = 0.0
        self._halted: bool = False
        self._halt_reason: str = ""
        self._last_reauth: ReAuthorization | None = None

    def update_equity(self, equity_usd: float) -> bool:
        """
        Records the latest equity mark and evaluates the floor. Returns
        True if trading should proceed (not halted), False if halted.
        Once halted, repeated calls keep returning False regardless of
        equity recovery — recovery alone never auto-clears the halt.

        Raises ValueError on a non-finite mark, which is *not* merely
        defensive here — it is the difference between a one-tick fault and a
        permanently disabled backstop:

          - `equity_usd < 0.0` is False for NaN, so the existing guard does
            not catch it, and `drawdown_pct` then computes to NaN.
            `nan >= max_drawdown_pct` is False, so the floor does not fire.
          - `inf` is worse and it persists. `max(peak, inf)` sets
            `_peak_equity = inf`, and from then on every drawdown is
            `(inf - equity) / inf = nan`, which never trips the floor again.
            One bad mark silently disables the outermost backstop for the
            life of the process.

        Raising keeps a corrupt mark out of `_peak_equity` entirely. The
        caller sees the fault instead of a floor that has quietly stopped
        working — this instance holds the only copy of that state, so there
        is nothing downstream that would notice.
        """
        if not math.isfinite(equity_usd):
            raise ValueError(f"equity_usd must be a finite number, got {equity_usd}")
        if equity_usd < 0.0:
            raise ValueError(f"equity_usd must be non-negative, got {equity_usd}")

        self._peak_equity = max(self._peak_equity, equity_usd)
        if self._halted:
            return False

        if self._peak_equity > 0.0:
            drawdown_pct = (self._peak_equity - equity_usd) / self._peak_equity
            if drawdown_pct >= self._max_drawdown_pct:
                self._halted = True
                self._halt_reason = (
                    f"drawdown {drawdown_pct:.3f} >= floor {self._max_drawdown_pct:.3f} "
                    f"(peak={self._peak_equity:.2f}, current={equity_usd:.2f})"
                )
                return False
        return True

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def re_authorize(self, authorized_by: str, reason: str, at_ms: int) -> None:
        """
        Explicit human re-authorization to resume trading. Does not reset
        peak_equity — a fresh peak is only established as new marks come
        in, so the floor remains sensitive to further drawdown from the
        pre-halt peak until equity genuinely recovers past it.
        """
        if not authorized_by:
            raise ValueError("authorized_by must be a non-empty string")
        self._halted = False
        self._halt_reason = ""
        self._last_reauth = ReAuthorization(authorized_by=authorized_by, reason=reason, at_ms=at_ms)

    @property
    def last_reauthorization(self) -> ReAuthorization | None:
        return self._last_reauth
