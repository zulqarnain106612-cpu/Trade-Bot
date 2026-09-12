"""
DATA-001, DATA-002, INV-008 — the data-quality gate.

The source document's checklist, in full:

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

Every line gets a test, and every test asserts on `result.reason` rather than
only on `passed` -- a gate that rejects for the wrong reason sends whoever
reads the log to the wrong place, and the reasons are what the audit trail
records.

The freshness budget gets its own section because it is the check that was
previously a constant. Five minutes is right for a realtime tick feed and
wrong for every other consumer: on a 15-minute timeframe the newest *closed*
bar is always at least fifteen minutes old, so the constant would have
rejected every well-formed frame the signal engine ever sees. That is why the
gate was never wired, and why wiring it required making the budget a
parameter (DATA-002).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from src.data.quality_gate import (
    MACRO_BUDGET,
    OHLCV_REALTIME_BUDGET,
    DataQualityGate,
    FreshnessBudget,
)


@pytest.fixture
def gate() -> DataQualityGate:
    return DataQualityGate()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)


def make_bars(n: int = 20, interval_s: int = 900, end: datetime | None = None) -> pd.DataFrame:
    """A well-formed OHLCV frame indexed by epoch milliseconds."""
    end = end or datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    end_ms = int(end.timestamp() * 1000)
    step = interval_s * 1000
    index = [end_ms - (n - 1 - i) * step for i in range(n)]
    close = np.linspace(50_000.0, 50_100.0, n)
    return pd.DataFrame(
        {
            "open": close - 5.0,
            "high": close + 10.0,
            "low": close - 10.0,
            "close": close,
            "volume": np.full(n, 1_000.0),
        },
        index=index,
    )


def make_frame(n: int = 20, interval_s: int = 60, end: datetime | None = None) -> pd.DataFrame:
    """A provider frame carrying a timestamp_utc column."""
    end = end or datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    stamps = [end - timedelta(seconds=interval_s * (n - 1 - i)) for i in range(n)]
    close = np.linspace(50_000.0, 50_100.0, n)
    return pd.DataFrame({"timestamp_utc": stamps, "close": close, "volume": np.full(n, 1_000.0)})


class TestAWellFormedFramePasses:
    def test_bars(self, gate, now):
        result = gate.check_bars(make_bars(), budget=FreshnessBudget.for_timeframe(900), now=now)
        assert result.passed, result.reason

    def test_provider_frame(self, gate, now):
        assert gate.check_ohlcv(make_frame(), now=now).passed

    def test_every_check_ran(self, gate, now):
        result = gate.check_bars(make_bars(), budget=FreshnessBudget.for_timeframe(900), now=now)
        assert result.checks_run == (
            "schema",
            "finite",
            "timestamp_valid",
            "monotonic",
            "duplicates",
            "ohlc_consistent",
            "volume_non_negative",
            "fresh",
            "gaps",
            "extreme_return",
            "zero_volume_run",
        )


class TestSchema:
    def test_an_empty_frame_is_refused(self, gate):
        assert not gate.check_ohlcv(pd.DataFrame()).passed
        assert not gate.check_bars(pd.DataFrame()).passed

    @pytest.mark.parametrize("column", ["close", "volume"])
    def test_a_missing_required_column_is_named(self, gate, now, column):
        bars = make_bars().drop(columns=[column])
        result = gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert not result.passed
        assert f"schema_missing_column: {column}" in result.reason

    def test_a_provider_frame_without_a_timestamp_column_is_refused(self, gate):
        frame = make_frame().drop(columns=["timestamp_utc"])
        result = gate.check_ohlcv(frame)
        assert not result.passed
        assert "timestamp_utc" in result.reason

    def test_a_close_and_volume_only_frame_is_still_validated(self, gate, now):
        # Several providers supply no OHLC. Inventing the missing columns
        # would be worse than skipping the check, so the OHLC check is
        # skipped and every other check still runs.
        assert gate.check_ohlcv(make_frame(), now=now).passed


class TestNoNaNAndNoInfinity:
    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf], ids=["nan", "inf", "-inf"])
    @pytest.mark.parametrize("column", ["open", "high", "low", "close", "volume"])
    def test_a_single_non_finite_value_is_refused(self, gate, now, column, bad):
        # The reason this is checked before the price-move check: every
        # comparison against NaN is False, so a NaN close does not trip the
        # extreme-return check -- it walks past it.
        bars = make_bars()
        bars.loc[bars.index[3], column] = bad
        result = gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert not result.passed
        assert "non_finite_values" in result.reason
        assert column in result.reason

    def test_a_non_numeric_close_reads_as_non_finite(self, gate, now):
        bars = make_bars()
        bars["close"] = bars["close"].astype(object)
        bars.loc[bars.index[2], "close"] = "n/a"
        result = gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert not result.passed
        assert "non_finite_values" in result.reason


class TestTimestamps:
    def test_an_unparseable_timestamp_is_refused(self, gate):
        frame = make_frame()
        # object dtype, because assigning a string into a datetime64 column
        # is refused by pandas itself -- this is the shape a provider frame
        # actually arrives in when the venue returns a malformed field.
        frame["timestamp_utc"] = frame["timestamp_utc"].astype(object)
        frame.loc[frame.index[0], "timestamp_utc"] = "not-a-date"
        result = gate.check_ohlcv(frame)
        assert not result.passed
        assert "timestamp" in result.reason

    def test_a_null_timestamp_is_refused(self, gate, now):
        # Parsing does not raise on a null -- it produces NaT, which then
        # compares False against every freshness bound. This is the check
        # that catches it.
        frame = make_frame()
        frame["timestamp_utc"] = frame["timestamp_utc"].astype(object)
        frame.loc[frame.index[2], "timestamp_utc"] = None
        result = gate.check_ohlcv(frame, now=now)
        assert not result.passed
        assert "timestamp_invalid: 1" in result.reason

    def test_an_index_that_is_not_a_timestamp_is_refused(self, gate, now):
        bars = make_bars()
        bars.index = [f"bar-{i}" for i in range(len(bars))]
        result = gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert not result.passed
        assert "timestamp_unparseable" in result.reason

    def test_out_of_order_bars_are_refused(self, gate, now):
        bars = make_bars()
        reordered = bars.iloc[[0, 2, 1, *range(3, len(bars))]]
        result = gate.check_bars(reordered, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert not result.passed
        assert result.reason == "timestamps_not_monotonic"

    def test_duplicate_timestamps_are_refused(self, gate, now):
        bars = make_bars()
        duplicated = pd.concat([bars, bars.iloc[[-1]]])
        result = gate.check_bars(duplicated, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert not result.passed
        assert "duplicate_timestamps: 1" in result.reason


class TestImpossibleOHLC:
    def test_high_below_low(self, gate, now):
        bars = make_bars()
        bars.loc[bars.index[4], "high"] = bars.loc[bars.index[4], "low"] - 1.0
        result = gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert not result.passed
        assert "high < low" in result.reason

    def test_high_below_the_close(self, gate, now):
        bars = make_bars()
        bars.loc[bars.index[4], "high"] = bars.loc[bars.index[4], "close"] - 1.0
        result = gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert not result.passed
        assert "high below open/close" in result.reason

    def test_low_above_the_open(self, gate, now):
        bars = make_bars()
        bars.loc[bars.index[4], "low"] = bars.loc[bars.index[4], "open"] + 1.0
        result = gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert not result.passed
        assert "low above open/close" in result.reason

    def test_a_non_positive_price(self, gate, now):
        bars = make_bars()
        bars.loc[bars.index[4], "low"] = -10.0
        bars.loc[bars.index[4], "open"] = 0.0
        result = gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert not result.passed
        assert "non-positive price" in result.reason


class TestVolume:
    def test_negative_volume_is_refused(self, gate, now):
        bars = make_bars()
        bars.loc[bars.index[7], "volume"] = -1.0
        result = gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert not result.passed
        assert "negative_volume: 1" in result.reason

    def test_a_run_of_zero_volume_is_refused(self, gate, now):
        bars = make_bars()
        bars.loc[bars.index[5:8], "volume"] = 0.0
        result = gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert not result.passed
        assert "zero_volume" in result.reason

    def test_two_zero_volume_bars_are_tolerated(self, gate, now):
        bars = make_bars()
        bars.loc[bars.index[5:7], "volume"] = 0.0
        assert gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900), now=now).passed


class TestFreshnessIsADeclaredBudget:
    """DATA-002 / INV-008."""

    def test_a_sample_inside_the_budget_is_current(self, gate, now):
        bars = make_bars(end=now - timedelta(seconds=1_700))
        assert gate.check_bars(
            bars, budget=FreshnessBudget.for_timeframe(900, bars=2), now=now
        ).passed

    def test_a_sample_past_the_budget_is_refused(self, gate, now):
        bars = make_bars(end=now - timedelta(seconds=1_900))
        result = gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900, bars=2), now=now)
        assert not result.passed
        assert "ohlcv_stale" in result.reason

    def test_the_rejection_names_the_budget_that_refused_it(self, gate, now):
        bars = make_bars(end=now - timedelta(days=1))
        result = gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900, bars=2), now=now)
        assert "ohlcv_900s_x2" in result.reason

    def test_the_realtime_default_would_reject_a_healthy_fifteen_minute_frame(self, gate, now):
        # The reason the gate could not simply be wired as it stood. Kept as a
        # test rather than a comment because it is the argument for DATA-002.
        bars = make_bars(interval_s=900, end=now - timedelta(seconds=900))
        assert not gate.check_bars(bars, budget=OHLCV_REALTIME_BUDGET, now=now).passed
        assert gate.check_bars(
            bars, budget=FreshnessBudget.for_timeframe(900, bars=2), now=now
        ).passed

    @pytest.mark.parametrize("bad", [0, -1, math.nan, math.inf])
    def test_a_budget_cannot_be_derived_from_a_nonsense_interval(self, bad):
        with pytest.raises(ValueError, match="timeframe_s"):
            FreshnessBudget.for_timeframe(bad)

    def test_a_derived_budget_is_the_interval_times_the_bar_count(self):
        assert FreshnessBudget.for_timeframe(900, bars=3).max_age_s == 2_700.0


class TestTheGapPolicy:
    def test_gaps_are_reported_and_not_blocking_by_default(self, gate, now):
        bars = make_bars(n=30)
        gapped = bars.drop(index=bars.index[10:14])
        result = gate.check_bars(gapped, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert result.passed, result.reason

    def test_a_budget_with_a_ceiling_makes_gaps_blocking(self, gate, now):
        bars = make_bars(n=30)
        gapped = bars.drop(index=bars.index[10:14])
        budget = FreshnessBudget(label="strict", max_age_s=2_700.0, max_gap_pct=0.5)
        result = gate.check_bars(gapped, budget=budget, now=now)
        assert not result.passed
        assert "bar_gaps" in result.reason

    def test_a_contiguous_frame_reports_no_gaps(self, gate):
        report = gate.gap_report(
            pd.to_datetime(make_bars(n=30).index.astype("int64"), unit="ms", utc=True).to_series()
        )
        assert report["gap_count"] == 0
        assert report["expected_s"] == 900.0

    def test_a_gapped_frame_counts_the_missing_bars(self, gate):
        bars = make_bars(n=30)
        gapped = bars.drop(index=bars.index[10:14])
        stamps = pd.to_datetime(gapped.index.astype("int64"), unit="ms", utc=True).to_series()
        report = gate.gap_report(stamps)
        assert report["gap_count"] == 1
        assert report["missing_bars"] == 4

    @pytest.mark.parametrize("n", [0, 1, 2])
    def test_too_few_points_have_no_inferable_spacing(self, gate, n):
        stamps = (
            pd.Series(pd.to_datetime([], utc=True))
            if n == 0
            else pd.to_datetime(
                make_bars(n=max(n, 1)).index[:n].astype("int64"), unit="ms", utc=True
            ).to_series()
        )
        assert gate.gap_report(stamps)["gap_count"] == 0

    def test_a_series_of_nulls_reports_nothing(self, gate):
        # Unreachable through `_validate`, which rejects null timestamps
        # earlier -- but `gap_report` is public, and returning a division by
        # an empty series to a direct caller would be worse than returning
        # "no inferable spacing".
        stamps = pd.Series(pd.to_datetime([None, None, None], utc=True))
        assert gate.gap_report(stamps)["gap_count"] == 0

    def test_a_degenerate_spacing_reports_nothing(self, gate):
        # Every diff is zero, so there is no interval to compare against.
        stamps = pd.Series(pd.to_datetime([0, 0, 0, 0], unit="ms", utc=True))
        assert gate.gap_report(stamps)["expected_s"] == 0


class TestExtremeReturns:
    def test_a_price_jump_beyond_the_budget_is_refused(self, gate, now):
        bars = make_bars()
        bars.loc[bars.index[-1], ["open", "high", "low", "close"]] = [
            100_000.0,
            100_010.0,
            99_990.0,
            100_000.0,
        ]
        result = gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900), now=now)
        assert not result.passed
        assert "extreme_return" in result.reason

    def test_the_threshold_is_part_of_the_declared_budget(self, gate, now):
        bars = make_bars()
        bars.loc[bars.index[-1], ["open", "high", "low", "close"]] = [
            60_000.0,
            60_010.0,
            59_990.0,
            60_000.0,
        ]
        tolerant = FreshnessBudget(label="tolerant", max_age_s=2_700.0, max_abs_return=0.5)
        assert gate.check_bars(bars, budget=tolerant, now=now).passed
        assert not gate.check_bars(bars, budget=FreshnessBudget.for_timeframe(900), now=now).passed


class TestTheOtherFeeds:
    def test_a_wide_spread_is_refused(self, gate):
        assert not gate.check_orderbook(250.0).passed

    def test_a_normal_spread_passes(self, gate):
        assert gate.check_orderbook(5.0).passed

    @pytest.mark.parametrize("bad", [math.nan, math.inf])
    def test_a_non_finite_spread_is_refused(self, gate, bad):
        result = gate.check_orderbook(bad)
        assert not result.passed
        assert "non_finite" in result.reason

    def test_zero_implied_volatility_is_refused(self, gate):
        assert not gate.check_options_row(iv=0.0, oi=100.0).passed

    def test_zero_open_interest_is_refused(self, gate):
        assert not gate.check_options_row(iv=0.5, oi=0.0).passed

    def test_a_real_options_row_passes(self, gate):
        assert gate.check_options_row(iv=0.5, oi=100.0).passed

    @pytest.mark.parametrize("bad", [math.nan, math.inf])
    def test_a_non_finite_options_row_is_refused(self, gate, bad):
        assert not gate.check_options_row(iv=bad, oi=100.0).passed

    def test_a_fresh_macro_row_passes(self, gate):
        row = {"date": (datetime.now(UTC) - timedelta(hours=6)).date().isoformat()}
        assert gate.check_macro(row).passed

    def test_a_stale_macro_row_is_refused(self, gate):
        row = {"date": (datetime.now(UTC) - timedelta(days=9)).date().isoformat()}
        result = gate.check_macro(row)
        assert not result.passed
        assert MACRO_BUDGET.label in result.reason

    def test_a_missing_macro_date_is_refused(self, gate):
        assert not gate.check_macro({}).passed

    def test_a_malformed_macro_date_is_refused(self, gate):
        assert not gate.check_macro({"date": "yesterday"}).passed

    def test_matching_prices_pass_cross_validation(self, gate):
        assert gate.check_price_deviation(50_000.0, 50_050.0).passed

    def test_a_diverging_secondary_source_is_refused(self, gate):
        result = gate.check_price_deviation(50_000.0, 51_000.0)
        assert not result.passed
        assert "cross_source_deviation" in result.reason

    def test_cross_validation_is_skipped_without_both_sides(self, gate):
        assert gate.check_price_deviation(0.0, 50_000.0).passed

    @pytest.mark.parametrize("bad", [math.nan, math.inf])
    def test_a_non_finite_price_is_refused(self, gate, bad):
        assert not gate.check_price_deviation(bad, 50_000.0).passed

    @pytest.mark.parametrize("score", [0.0, 50.0, 100.0])
    def test_a_sentiment_score_in_range_passes(self, gate, score):
        assert gate.check_sentiment_score(score).passed

    @pytest.mark.parametrize("score", [-0.1, 100.1])
    def test_a_sentiment_score_out_of_range_is_refused(self, gate, score):
        assert not gate.check_sentiment_score(score).passed

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_a_non_finite_sentiment_score_is_refused(self, gate, bad):
        # `0.0 <= nan <= 100.0` is False, so the range check would have
        # rejected it anyway -- but for the wrong reason, and the reason is
        # what the audit trail records.
        result = gate.check_sentiment_score(bad)
        assert not result.passed
        assert "non_finite" in result.reason


class TestRejectionsAreAudited:
    def test_a_rejection_records_an_audit_event(self, gate, monkeypatch):
        recorded: list[dict] = []

        class _Trail:
            def record(self, **kwargs):
                recorded.append(kwargs)

        import src.diagnostics.audit_trail as audit

        monkeypatch.setattr(audit, "get_audit_trail", lambda: _Trail())
        gate.check_orderbook(500.0)
        assert recorded and recorded[0]["event_type"] == "data_quality_reject"

    def test_an_unavailable_audit_trail_does_not_block_validation(self, gate, monkeypatch):
        import src.diagnostics.audit_trail as audit

        def _boom():
            raise RuntimeError("audit down")

        monkeypatch.setattr(audit, "get_audit_trail", _boom)
        # The rejection still happens; only the record of it is lost, and that
        # is logged. Validation must not depend on the audit trail being up.
        assert not gate.check_orderbook(500.0).passed
