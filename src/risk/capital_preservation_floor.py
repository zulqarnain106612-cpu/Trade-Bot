"""
Capital preservation floor — trailing high-water mark with floor enforcement.

Prevents giving back accumulated gains beyond a configurable percentage of
the high-water mark (HWM). Two modes of operation:

  1. Ratchet floor (trailing stop on equity):
     Once equity exceeds HWM * (1 + trigger_pct), set a floor at
     HWM * (1 + lock_in_pct). If equity falls below the floor,
     block new positions (not a hard liquidation — that's the executor's job).

  2. Initial capital preservation:
     Never allow equity to fall below initial_capital * (1 - max_loss_pct).
     This is independent of the ratchet and fires earlier in a new account.

Both operate as passive gates: they return a GateStatus that the signal
engine checks before opening a new position. Closing existing positions
is not gated here — only new entries are blocked.

Authority:
  Vince (1992) The Mathematics of Money Management — drawdown control.
  Carver (2019) Systematic Trading Ch.5 — capital protection rules.
  Chan (2013) Algorithmic Trading Ch.6 — account-level risk limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_DEFAULT_TRIGGER_PCT: Final[float] = 0.10  # activate ratchet after +10% gain
_DEFAULT_LOCK_IN_PCT: Final[float] = 0.05  # lock in at least +5% above start
_DEFAULT_MAX_LOSS_PCT: Final[float] = 0.20  # never lose more than 20% of initial


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FloorCheckResult:
    """Result of a capital preservation floor check."""

    allowed: bool  # True = new entry is permitted
    reason: str  # empty when allowed
    current_equity: float
    hwm: float
    floor: float  # current active floor (0 if not yet triggered)
    ratchet_active: bool  # whether the ratchet has been triggered
    gain_since_start_pct: float
    drawdown_from_hwm_pct: float

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "current_equity": round(self.current_equity, 2),
            "hwm": round(self.hwm, 2),
            "floor": round(self.floor, 2),
            "ratchet_active": self.ratchet_active,
            "gain_since_start_pct": round(self.gain_since_start_pct, 4),
            "drawdown_from_hwm_pct": round(self.drawdown_from_hwm_pct, 4),
        }


# ---------------------------------------------------------------------------
# Floor tracker
# ---------------------------------------------------------------------------


class CapitalPreservationFloor:
    """
    High-water mark ratchet with two-layer capital protection.

    Parameters
    ----------
    initial_capital:
        Starting equity in USD. Used for the initial max-loss gate.
    trigger_pct:
        Ratchet activates once equity gains >= trigger_pct above initial.
    lock_in_pct:
        Floor is set at initial * (1 + lock_in_pct) once ratchet fires.
        Must be < trigger_pct to make sense (we lock in gains, not losses).
    max_loss_pct:
        Hard floor as a fraction of initial capital. Always active.
        E.g. 0.20 → never let equity fall below 80% of initial.
    """

    def __init__(
        self,
        initial_capital: float,
        trigger_pct: float = _DEFAULT_TRIGGER_PCT,
        lock_in_pct: float = _DEFAULT_LOCK_IN_PCT,
        max_loss_pct: float = _DEFAULT_MAX_LOSS_PCT,
    ) -> None:
        if initial_capital <= 0:
            raise ValueError(f"initial_capital must be positive, got {initial_capital}")
        if lock_in_pct >= trigger_pct:
            raise ValueError(
                f"lock_in_pct ({lock_in_pct}) must be < trigger_pct ({trigger_pct}) "
                "— the locked-in gain must be less than the trigger threshold"
            )
        if not 0 < max_loss_pct < 1:
            raise ValueError(f"max_loss_pct must be in (0, 1), got {max_loss_pct}")

        self._initial = initial_capital
        self._trigger_pct = trigger_pct
        self._lock_in_pct = lock_in_pct
        self._max_loss_pct = max_loss_pct

        self._hwm: float = initial_capital
        self._floor: float = 0.0  # 0 = not yet active
        self._ratchet_active: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, equity: float) -> FloorCheckResult:
        """
        Update HWM and ratchet state, then evaluate all floor gates.

        Parameters
        ----------
        equity:
            Current account equity in USD.

        Returns
        -------
        FloorCheckResult — query ``.allowed`` before opening a position.
        """
        # Update HWM
        if equity > self._hwm:
            self._hwm = equity

        # Activate ratchet if gain threshold crossed
        gain_pct = (equity - self._initial) / self._initial
        if not self._ratchet_active and gain_pct >= self._trigger_pct:
            self._ratchet_active = True
            self._floor = self._initial * (1.0 + self._lock_in_pct)
            log.info(
                "capital_floor.ratchet_activated",
                equity=round(equity, 2),
                floor=round(self._floor, 2),
                gain_pct=round(gain_pct, 4),
            )

        # Compute metrics
        dd_from_hwm = (self._hwm - equity) / self._hwm if self._hwm > 0 else 0.0
        absolute_floor = self._initial * (1.0 - self._max_loss_pct)

        # Gate 1: absolute max-loss floor (always active)
        if equity < absolute_floor:
            reason = (
                f"equity={equity:.2f} < absolute_floor={absolute_floor:.2f} "
                f"(initial={self._initial:.2f}, max_loss={self._max_loss_pct:.0%})"
            )
            log.warning("capital_floor.absolute_floor_breach", reason=reason)
            return self._result(False, reason, equity, dd_from_hwm, gain_pct)

        # Gate 2: ratchet floor (active only after trigger)
        if self._ratchet_active and equity < self._floor:
            reason = (
                f"equity={equity:.2f} < ratchet_floor={self._floor:.2f} "
                f"(locked_in={self._lock_in_pct:.0%} of initial)"
            )
            log.warning("capital_floor.ratchet_floor_breach", reason=reason)
            return self._result(False, reason, equity, dd_from_hwm, gain_pct)

        return self._result(True, "", equity, dd_from_hwm, gain_pct)

    def reset(self, new_initial_capital: float | None = None) -> None:
        """
        Reset the floor tracker.

        Call this when the account is refilled or when a new trading session
        starts. If ``new_initial_capital`` is None, resets to the original
        initial capital.
        """
        cap = new_initial_capital if new_initial_capital is not None else self._initial
        if cap > 0:
            self._initial = cap
        self._hwm = self._initial
        self._floor = 0.0
        self._ratchet_active = False

    @property
    def hwm(self) -> float:
        return self._hwm

    @property
    def floor(self) -> float:
        return self._floor

    @property
    def ratchet_active(self) -> bool:
        return self._ratchet_active

    def state_dict(self) -> dict:
        return {
            "initial_capital": self._initial,
            "hwm": self._hwm,
            "floor": self._floor,
            "ratchet_active": self._ratchet_active,
            "trigger_pct": self._trigger_pct,
            "lock_in_pct": self._lock_in_pct,
            "max_loss_pct": self._max_loss_pct,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _result(
        self,
        allowed: bool,
        reason: str,
        equity: float,
        dd_from_hwm: float,
        gain_since_start_pct: float,
    ) -> FloorCheckResult:
        return FloorCheckResult(
            allowed=allowed,
            reason=reason,
            current_equity=equity,
            hwm=self._hwm,
            floor=self._floor,
            ratchet_active=self._ratchet_active,
            gain_since_start_pct=gain_since_start_pct,
            drawdown_from_hwm_pct=dd_from_hwm,
        )
