"""
Lookahead detection — the highest-value test in this project.

MODL-003. The premise, from the source document:

```
Dataset A = data through T
Dataset B = same data through T + arbitrary future data

signal_A(T) == signal_B(T)
```

If not:

```
❌ LOOKAHEAD DETECTED
```

A backtest with lookahead is not merely optimistic; it is a fantasy, and the
live strategy then loses money in a way the simulation never showed. The
failure is silent by construction — a leaking feature makes the backtest look
*better*, so nothing about the result invites suspicion.

Two checks, because they catch different things:

- :func:`detect_lookahead` extends the series with **real** future bars. This
  catches any calculation that fits, centres or normalises over the whole
  series: a z-score against the full-sample mean, a scaler fitted on
  everything, a `bfill`.
- :func:`detect_future_poisoning` extends it with **absurd** future bars. This
  catches the same class more loudly, and additionally catches a calculation
  that is only weakly whole-sample — one whose dependence on the future is
  too small to see against real data but obvious against a 10x price spike.

Both return a report rather than raising, because the caller decides whether a
detection is a failed release gate or a line in a research notebook.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

#: Exact equality is the right default. A feature that differs in the last bit
#: when the future changes is still reading the future; the size of the
#: difference is a fact about the arithmetic, not about the leak.
DEFAULT_TOLERANCE: float = 0.0

FeatureBuilder = Callable[[pd.DataFrame], pd.DataFrame]


@dataclass(frozen=True)
class LookaheadReport:
    """The result of one lookahead comparison."""

    #: Rows the two computations had in common and therefore compared.
    compared_rows: int
    #: Largest absolute difference seen on any shared row, any column.
    max_abs_diff: float
    #: Columns whose values changed when the future changed, worst first.
    offending_columns: tuple[str, ...]
    tolerance: float
    #: Columns present in one computation and not the other. A changing
    #: column *set* is a leak too, and comparing only the intersection would
    #: hide it.
    column_set_changed: bool = False

    @property
    def detected(self) -> bool:
        return bool(self.offending_columns) or self.column_set_changed

    @property
    def reason(self) -> str:
        if not self.detected:
            return ""
        if self.column_set_changed:
            return (
                "LOOKAHEAD DETECTED: the feature column set changed when future "
                "bars were added, so the shape of the model's input depends on "
                "data that did not exist at decision time."
            )
        return (
            f"LOOKAHEAD DETECTED: {len(self.offending_columns)} column(s) changed "
            f"when future bars were added -- {', '.join(self.offending_columns[:5])} "
            f"-- with a maximum difference of {self.max_abs_diff:g} over "
            f"{self.compared_rows} shared rows (tolerance {self.tolerance:g})."
        )


def _compare(
    past: pd.DataFrame,
    extended: pd.DataFrame,
    tolerance: float,
) -> LookaheadReport:
    """Compare the rows two feature frames share."""
    if set(past.columns) != set(extended.columns):
        return LookaheadReport(
            compared_rows=0,
            max_abs_diff=float("inf"),
            offending_columns=tuple(
                sorted(set(past.columns).symmetric_difference(extended.columns))
            ),
            tolerance=tolerance,
            column_set_changed=True,
        )

    shared = past.index.intersection(extended.index)
    if len(shared) == 0:
        return LookaheadReport(
            compared_rows=0,
            max_abs_diff=0.0,
            offending_columns=(),
            tolerance=tolerance,
        )

    left = past.loc[shared, list(past.columns)].to_numpy(dtype=np.float64)
    right = extended.loc[shared, list(past.columns)].to_numpy(dtype=np.float64)

    # NaN in the same place in both is agreement, not a difference. Treating
    # it as one would make every burn-in row a false positive.
    both_nan = np.isnan(left) & np.isnan(right)
    diff = np.abs(left - right)
    diff[both_nan] = 0.0
    # NaN in exactly one of them is a genuine disagreement, and `abs(nan - x)`
    # is NaN rather than large -- so it has to be made large deliberately.
    diff[np.isnan(diff)] = np.inf

    per_column = np.nanmax(diff, axis=0) if len(diff) else np.zeros(len(past.columns))
    offenders = [
        (float(per_column[i]), column)
        for i, column in enumerate(past.columns)
        if per_column[i] > tolerance
    ]
    offenders.sort(reverse=True)

    return LookaheadReport(
        compared_rows=int(len(shared)),
        max_abs_diff=float(per_column.max()) if len(per_column) else 0.0,
        offending_columns=tuple(name for _, name in offenders),
        tolerance=tolerance,
    )


def detect_lookahead(
    bars: pd.DataFrame,
    compute: FeatureBuilder,
    future_bars: int = 50,
    tolerance: float = DEFAULT_TOLERANCE,
) -> LookaheadReport:
    """
    Compare features computed through T against features computed through T+k.

    Parameters
    ----------
    bars        : the full series. The last ``future_bars`` rows play the part
                  of "the future"; everything before them is dataset A.
    compute     : builds the feature frame from bars. Must be the *real*
                  production builder -- a simplified stand-in tests the
                  stand-in.
    future_bars : how many rows to withhold from dataset A.
    tolerance   : absolute difference treated as agreement. Zero by default.
    """
    if future_bars <= 0:
        raise ValueError(f"future_bars must be positive, got {future_bars}")
    if len(bars) <= future_bars:
        raise ValueError(
            f"bars has {len(bars)} rows and future_bars is {future_bars}: "
            "there would be no past to compare."
        )

    past = compute(bars.iloc[:-future_bars])
    extended = compute(bars)
    return _compare(past, extended, tolerance)


def detect_future_poisoning(
    bars: pd.DataFrame,
    compute: FeatureBuilder,
    future_bars: int = 50,
    multiplier: float = 10.0,
    tolerance: float = DEFAULT_TOLERANCE,
) -> LookaheadReport:
    """
    The louder version: replace the future with something absurd.

    The source document says "the same data through T + **arbitrary** future
    data". Arbitrary is the operative word -- real future data resembles the
    past, so a weak dependence on it can hide inside the noise. A tenfold
    price spike cannot hide.

    ``multiplier`` scales the price columns of the withheld tail; volume is
    scaled with them so the frame stays internally consistent and the OHLC
    relationships still hold.
    """
    if multiplier <= 0 or not np.isfinite(multiplier):
        raise ValueError(f"multiplier must be a positive finite number, got {multiplier}")
    if len(bars) <= future_bars:
        raise ValueError(
            f"bars has {len(bars)} rows and future_bars is {future_bars}: "
            "there would be no past to compare."
        )

    poisoned = bars.copy(deep=True)
    tail = poisoned.index[-future_bars:]
    for column in ("open", "high", "low", "close"):
        if column in poisoned.columns:
            poisoned.loc[tail, column] = poisoned.loc[tail, column] * multiplier
    if "volume" in poisoned.columns:
        poisoned.loc[tail, "volume"] = poisoned.loc[tail, "volume"] * multiplier

    past = compute(bars.iloc[:-future_bars])
    extended = compute(poisoned)
    return _compare(past, extended, tolerance)
