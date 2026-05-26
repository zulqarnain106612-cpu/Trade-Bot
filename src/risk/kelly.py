"""
Kelly criterion position sizing.
Fractional Kelly (half-Kelly) used for robustness.

Reference: Kelly, J.L. (1956). A New Interpretation of Information Rate.
           Bell System Technical Journal, 35(4), 917-926.
           Thorp, E.O. (2006). The Kelly Criterion in Blackjack, Sports Betting,
           and the Stock Market. Handbook of Asset and Liability Management.
"""
from __future__ import annotations
import numpy as np
import structlog

log = structlog.get_logger()

HALF_KELLY = 0.5   # fractional Kelly multiplier — reduces drawdown significantly

def kelly_fraction(
    win_prob:     float,
    avg_win:      float,
    avg_loss:     float,
    half_kelly:   float = HALF_KELLY,
) -> float:
    """
    Compute Kelly fraction.
    f* = (p * b - q) / b
    where p = win_prob, q = 1-p, b = avg_win / avg_loss

    Returns clamped value in [0, 0.25] — never bet more than 25% even at maximum Kelly.
    """
    if avg_loss <= 0 or avg_win <= 0:
        return 0.0
    q = 1.0 - win_prob
    b = avg_win / avg_loss
    f = (win_prob * b - q) / b
    f_fractional = f * half_kelly
    return float(np.clip(f_fractional, 0.0, 0.25))


def size_position(
    capital:      float,
    kelly_frac:   float,
    entry_price:  float,
    max_pct:      float = 0.05,
) -> tuple[float, float]:
    """
    Convert Kelly fraction to position size.
    Returns (qty, notional_usd).
    Hard cap at max_pct of capital.
    """
    notional = capital * min(kelly_frac, max_pct)
    qty      = notional / entry_price if entry_price > 0 else 0.0
    return float(qty), float(notional)


class KellySizer:
    """Stateful sizer that tracks trade history to update win/loss stats."""

    def __init__(self, lookback: int = 50):
        self._lookback = lookback
        self._results:  list[float] = []   # list of pnl_pct values

    def record(self, pnl_pct: float):
        self._results.append(pnl_pct)
        if len(self._results) > self._lookback:
            self._results.pop(0)

    def fraction(self) -> float:
        if len(self._results) < 10:
            return 0.005   # ultra-conservative before history exists
        wins  = [r for r in self._results if r > 0]
        loses = [r for r in self._results if r <= 0]
        if not wins or not loses:
            return 0.005
        win_prob = len(wins) / len(self._results)
        avg_win  = float(np.mean(wins))
        avg_loss = float(abs(np.mean(loses)))
        frac = kelly_fraction(win_prob, avg_win, avg_loss)
        log.debug("kelly fraction", win_prob=round(win_prob,3), avg_win=round(avg_win,4),
                  avg_loss=round(avg_loss,4), fraction=round(frac,4))
        return frac

