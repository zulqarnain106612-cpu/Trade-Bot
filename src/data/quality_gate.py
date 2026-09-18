"""
Data quality gate — validates all incoming data before engine consumption.

DATA-001, DATA-002, INV-008.

Before market data reaches the signal engine it must clear every check in this
module:

```
schema valid
timestamp valid
monotonic
not stale
no impossible OHLC
no negative volume
no NaN
no infinity
duplicate policy
gap policy
```

If invalid: **NO TRADE**, unless the declared policy says otherwise. There is
exactly one such exception and it is named: the gap policy defaults to
reporting rather than rejecting, because OHLCV gaps are real -- exchange
outages, halts, truncated pagination -- and the feature pipeline already
reports them without fabricating prices that never traded. Set
``max_gap_pct`` on the budget to make gaps blocking.

Two entry points, because market data arrives in two shapes:

- :meth:`DataQualityGate.check_ohlcv` for a provider frame with a
  ``timestamp_utc`` column.
- :meth:`DataQualityGate.check_bars` for the engine's frame, indexed by
  integer epoch milliseconds.

Both run the same checks in the same order; only the timestamp extraction
differs.

**Freshness is a declared budget, not a constant** (DATA-002). The five-minute
default suits a realtime tick feed and is wrong for every other consumer: on a
15-minute timeframe the most recent *closed* bar is always at least fifteen
minutes old, so a five-minute budget would reject every well-formed frame.
Callers state the budget they actually need, and
:meth:`FreshnessBudget.for_timeframe` derives it from the bar interval.

All rejections log to audit_trail.py.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The minimum a frame must carry to be checkable at all. OHLC columns are
#: checked for internal consistency when present; a close-and-volume frame from
#: a provider that supplies nothing else is still validated for everything the
#: columns it does have can express.
REQUIRED_OHLCV_COLUMNS: tuple[str, ...] = ("close", "volume")
OHLC_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close")

#: Consecutive zero-volume candles that indicate a dead or halted feed.
_ZERO_VOLUME_RUN: int = 3


@dataclass(frozen=True)
class FreshnessBudget:
    """
    How old the most recent sample may be before it stops being current.

    ``label`` exists so a rejection names the budget that refused it. A
    rejection that says only "stale" sends the reader to the wrong place when
    three different budgets are in play.
    """

    label: str
    max_age_s: float
    #: Percentage of bar intervals that may be missing before the frame is
    #: rejected. ``None`` means gaps are reported and not blocking, which is
    #: the default and is a deliberate policy rather than an oversight.
    max_gap_pct: float | None = None
    #: Absolute per-bar return above which the frame is treated as corrupt
    #: rather than volatile.
    max_abs_return: float = 0.15

    @classmethod
    def for_timeframe(cls, timeframe_s: float, bars: float = 2.0) -> FreshnessBudget:
        """
        A budget derived from the bar interval.

        The newest *closed* bar on an N-second timeframe is between N and 2N
        seconds old in normal operation, so a budget of two bars is the
        tightest one that does not reject healthy data, and three or more
        starts tolerating a genuinely stalled feed.
        """
        if not math.isfinite(timeframe_s) or timeframe_s <= 0:
            raise ValueError(f"timeframe_s must be a positive number, got {timeframe_s!r}")
        return cls(label=f"ohlcv_{int(timeframe_s)}s_x{bars:g}", max_age_s=timeframe_s * bars)


#: The historical default: a realtime tick feed, five minutes.
OHLCV_REALTIME_BUDGET = FreshnessBudget(label="ohlcv_realtime", max_age_s=300.0)

#: Macro series print on business days, so a weekend is not staleness.
MACRO_BUDGET = FreshnessBudget(label="macro_daily", max_age_s=2 * 86_400.0)


@dataclass
class QualityResult:
    passed: bool
    reason: str = ""
    #: Names of the checks that actually ran, in order. A frame missing OHLC
    #: columns is not checked for OHLC consistency, and a caller that needs to
    #: know that can see it here rather than infer it.
    checks_run: tuple[str, ...] = field(default_factory=tuple)


class DataQualityGate:
    """Validates data feeds before they reach the engine layer."""

    # ------------------------------------------------------------------
    # OHLCV
    # ------------------------------------------------------------------

    def check_ohlcv(
        self,
        df: pd.DataFrame,
        budget: FreshnessBudget = OHLCV_REALTIME_BUDGET,
        now: datetime | None = None,
    ) -> QualityResult:
        """Validate a provider frame carrying a ``timestamp_utc`` column."""
        if df.empty:
            return QualityResult(False, "empty_dataframe")
        if "timestamp_utc" not in df.columns:
            return self._reject("schema_missing_column: timestamp_utc")
        try:
            timestamps = pd.to_datetime(df["timestamp_utc"], utc=True)
        except (ValueError, TypeError) as exc:
            return self._reject(f"timestamp_unparseable: {exc}")
        return self._validate(df, timestamps, budget, now)

    def check_bars(
        self,
        bars: pd.DataFrame,
        budget: FreshnessBudget = OHLCV_REALTIME_BUDGET,
        now: datetime | None = None,
    ) -> QualityResult:
        """
        Validate the engine's frame: OHLCV columns, integer-ms index.

        This is the shape that actually reaches the signal engine, which is
        why it gets its own entry point rather than being coerced into the
        provider shape at the call site -- a coercion is one more place for
        the timestamp to be misread.
        """
        if bars.empty:
            return QualityResult(False, "empty_dataframe")
        try:
            timestamps = pd.to_datetime(bars.index.astype("int64"), unit="ms", utc=True)
        except (ValueError, TypeError, OverflowError) as exc:
            return self._reject(f"timestamp_unparseable: {exc}")
        return self._validate(bars, pd.Series(timestamps, index=bars.index), budget, now)

    # ------------------------------------------------------------------
    # The shared checklist
    # ------------------------------------------------------------------

    def _validate(
        self,
        df: pd.DataFrame,
        timestamps: pd.Series,
        budget: FreshnessBudget,
        now: datetime | None,
    ) -> QualityResult:
        """
        Run every check in order, stopping at the first failure.

        Order is deliberate and runs cheapest-and-most-fundamental first:
        there is no point asking whether a price move is extreme in a frame
        whose closes are NaN, and the reason string should name the root
        problem rather than a symptom of it.
        """
        ran: list[str] = []
        checks: Sequence[tuple[str, Any]] = (
            ("schema", lambda: self._check_schema(df)),
            ("finite", lambda: self._check_finite(df)),
            ("timestamp_valid", lambda: self._check_timestamps_present(timestamps)),
            ("monotonic", lambda: self._check_monotonic(timestamps)),
            ("duplicates", lambda: self._check_duplicates(timestamps)),
            ("ohlc_consistent", lambda: self._check_ohlc(df)),
            ("volume_non_negative", lambda: self._check_volume(df)),
            ("fresh", lambda: self._check_staleness(timestamps, budget, now)),
            ("gaps", lambda: self._check_gaps(timestamps, budget)),
            ("extreme_return", lambda: self._check_extreme_return(df, budget)),
            ("zero_volume_run", lambda: self._check_zero_volume_run(df)),
        )
        for name, check in checks:
            ran.append(name)
            reason = check()
            if reason is not None:
                result = self._reject(reason)
                result.checks_run = tuple(ran)
                return result
        return QualityResult(True, checks_run=tuple(ran))

    @staticmethod
    def _check_schema(df: pd.DataFrame) -> str | None:
        missing = [c for c in REQUIRED_OHLCV_COLUMNS if c not in df.columns]
        if missing:
            return f"schema_missing_column: {', '.join(missing)}"
        return None

    @staticmethod
    def _numeric_columns(df: pd.DataFrame) -> list[str]:
        candidates = (*OHLC_COLUMNS, "volume")
        return [c for c in candidates if c in df.columns]

    def _check_finite(self, df: pd.DataFrame) -> str | None:
        """
        No NaN and no infinity, in one check.

        Both are listed separately in the source document and both have the
        same consequence here: every later comparison against them is False,
        so an unguarded NaN close does not trip the extreme-return check --
        it walks past it, exactly as it does in the risk gates.
        """
        for column in self._numeric_columns(df):
            values = pd.to_numeric(df[column], errors="coerce").to_numpy(dtype="float64")
            if not np.all(np.isfinite(values)):
                bad = int((~np.isfinite(values)).sum())
                return f"non_finite_values: {bad} in {column}"
        return None

    @staticmethod
    def _check_timestamps_present(timestamps: pd.Series) -> str | None:
        if timestamps.isna().any():
            return f"timestamp_invalid: {int(timestamps.isna().sum())} unparseable"
        return None

    @staticmethod
    def _check_monotonic(timestamps: pd.Series) -> str | None:
        if not timestamps.is_monotonic_increasing:
            return "timestamps_not_monotonic"
        return None

    @staticmethod
    def _check_duplicates(timestamps: pd.Series) -> str | None:
        duplicated = int(timestamps.duplicated().sum())
        if duplicated:
            return f"duplicate_timestamps: {duplicated}"
        return None

    @staticmethod
    def _check_ohlc(df: pd.DataFrame) -> str | None:
        """
        Impossible OHLC: the bar does not describe a real interval.

        Skipped when the frame does not carry all four columns, which is the
        honest thing to do rather than inventing them -- several providers
        supply close and volume only.
        """
        if not all(c in df.columns for c in OHLC_COLUMNS):
            return None
        high = df["high"].to_numpy(dtype="float64")
        low = df["low"].to_numpy(dtype="float64")
        open_ = df["open"].to_numpy(dtype="float64")
        close = df["close"].to_numpy(dtype="float64")

        if np.any(high < low):
            return f"impossible_ohlc: high < low in {int((high < low).sum())} bar(s)"
        upper = np.maximum(open_, close)
        lower = np.minimum(open_, close)
        if np.any(high < upper):
            return f"impossible_ohlc: high below open/close in {int((high < upper).sum())} bar(s)"
        if np.any(low > lower):
            return f"impossible_ohlc: low above open/close in {int((low > lower).sum())} bar(s)"
        if np.any(close <= 0.0) or np.any(open_ <= 0.0):
            return "impossible_ohlc: non-positive price"
        return None

    @staticmethod
    def _check_volume(df: pd.DataFrame) -> str | None:
        # `volume` is in REQUIRED_OHLCV_COLUMNS and _check_schema runs first,
        # so its presence is guaranteed by the time this is reached.
        volume = df["volume"].to_numpy(dtype="float64")
        if np.any(volume < 0.0):
            return f"negative_volume: {int((volume < 0.0).sum())} bar(s)"
        return None

    def _check_staleness(
        self,
        timestamps: pd.Series,
        budget: FreshnessBudget,
        now: datetime | None,
    ) -> str | None:
        reference = now if now is not None else datetime.now(UTC)
        age = reference - timestamps.iloc[-1].to_pydatetime()
        if age > timedelta(seconds=budget.max_age_s):
            return (
                f"ohlcv_stale: {age.total_seconds():.0f}s old, budget "
                f"{budget.label}={budget.max_age_s:.0f}s"
            )
        return None

    def _check_gaps(self, timestamps: pd.Series, budget: FreshnessBudget) -> str | None:
        """
        The gap policy, declared rather than implicit.

        Every rolling window downstream counts bars, not time, so a gapped
        series quietly changes what those windows mean. Repairing the gap
        would fabricate prices that never traded, so the policy is to measure
        it and let the caller decide: reporting by default, blocking when the
        budget names a ceiling.
        """
        report = self.gap_report(timestamps)
        if not report["gap_count"]:
            return None
        if budget.max_gap_pct is not None and report["gap_pct"] > budget.max_gap_pct:
            return (
                f"bar_gaps: {report['gap_pct']:.2f}% of intervals exceed "
                f"{budget.max_gap_pct:.2f}% ({report['missing_bars']} bars missing)"
            )
        log.warning("data_quality.bar_gaps", budget=budget.label, **report)
        return None

    @staticmethod
    def gap_report(timestamps: pd.Series) -> dict[str, float | int]:
        """
        Count intervals materially longer than the modal spacing.

        The expected spacing is inferred rather than passed in, so this stays
        correct for a caller that resamples or stitches venues together. A
        half-interval tolerance keeps exchange timestamp jitter from reading
        as a gap.
        """
        empty: dict[str, float | int] = {
            "expected_s": 0,
            "gap_count": 0,
            "gap_pct": 0.0,
            "missing_bars": 0,
        }
        if len(timestamps) < 3:
            return empty
        diffs = timestamps.diff().dropna().dt.total_seconds()
        if diffs.empty:
            return empty
        expected = float(diffs.mode().iloc[0])
        if expected <= 0:
            return empty
        over = diffs[diffs > expected * 1.5]
        return {
            "expected_s": expected,
            "gap_count": len(over),
            "gap_pct": round(len(over) / len(diffs) * 100.0, 3),
            "missing_bars": int((over // expected - 1).sum()) if len(over) else 0,
        }

    @staticmethod
    def _check_extreme_return(df: pd.DataFrame, budget: FreshnessBudget) -> str | None:
        returns = df["close"].pct_change().dropna()
        if (returns.abs() > budget.max_abs_return).any():
            return f"ohlcv_extreme_return: |ret| > {budget.max_abs_return:.0%}"
        return None

    @staticmethod
    def _check_zero_volume_run(df: pd.DataFrame) -> str | None:
        # As above: _check_schema guarantees the column exists.
        run = (df["volume"] == 0).rolling(_ZERO_VOLUME_RUN).sum().max()
        if run is not None and run >= _ZERO_VOLUME_RUN:
            return f"ohlcv_zero_volume: {_ZERO_VOLUME_RUN} consecutive zero-volume candles"
        return None

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------

    def check_orderbook(self, spread_bps: float) -> QualityResult:
        if not math.isfinite(spread_bps):
            return self._reject(f"orderbook_non_finite_spread: {spread_bps}")
        if spread_bps > 200:
            return self._reject(f"orderbook_wide_spread: {spread_bps:.1f} bps")
        return QualityResult(True)

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    def check_options_row(self, iv: float, oi: float) -> QualityResult:
        if not math.isfinite(iv) or not math.isfinite(oi):
            return self._reject("options_non_finite")
        if iv == 0.0:
            return self._reject("options_zero_iv")
        if oi == 0.0:
            return self._reject("options_zero_oi")
        return QualityResult(True)

    # ------------------------------------------------------------------
    # Macro (stale up to 2 days — weekends)
    # ------------------------------------------------------------------

    def check_macro(
        self,
        row: dict[str, Any],
        budget: FreshnessBudget = MACRO_BUDGET,
    ) -> QualityResult:
        date_str = row.get("date", "")
        if not date_str:
            return self._reject("macro_missing_date")
        try:
            date = datetime.fromisoformat(str(date_str)).replace(tzinfo=UTC)
        except ValueError:
            return self._reject("macro_bad_date_format")
        age = datetime.now(UTC) - date
        if age > timedelta(seconds=budget.max_age_s):
            return self._reject(f"macro_stale: {age.days} days old, budget {budget.label}")
        return QualityResult(True)

    # ------------------------------------------------------------------
    # Cross-validation: Binance mid vs secondary source
    # ------------------------------------------------------------------

    def check_price_deviation(self, primary_mid: float, secondary_mid: float) -> QualityResult:
        if not math.isfinite(primary_mid) or not math.isfinite(secondary_mid):
            return self._reject("cross_source_non_finite")
        if primary_mid <= 0 or secondary_mid <= 0:
            return QualityResult(True)  # can't validate without both
        dev = abs(primary_mid - secondary_mid) / primary_mid
        if dev > 0.005:
            return self._reject(f"cross_source_deviation: {dev * 100:.2f}% vs secondary")
        return QualityResult(True)

    # ------------------------------------------------------------------
    # Sentiment
    # ------------------------------------------------------------------

    def check_sentiment_score(self, fg_score: float) -> QualityResult:
        # Checked before the range comparison, which NaN passes by being False
        # on both sides of the `<=` chain.
        if not math.isfinite(fg_score):
            return self._reject(f"sentiment_non_finite: {fg_score}")
        if not (0.0 <= fg_score <= 100.0):
            return self._reject(f"sentiment_out_of_range: {fg_score}")
        return QualityResult(True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reject(self, reason: str) -> QualityResult:
        log.warning("data_quality_reject", reason=reason)
        self._try_audit(reason)
        return QualityResult(False, reason)

    @staticmethod
    def _try_audit(reason: str) -> None:
        try:
            from src.diagnostics.audit_trail import get_audit_trail

            get_audit_trail().record(
                event_type="data_quality_reject",
                reason_code=reason[:120],
            )
        except Exception as exc:
            # Audit trail unavailable — must not block data validation, but the
            # rejection still needs to be visible somewhere.
            log.warning("audit_trail_record_failed", reason=reason[:120], exc=str(exc))
