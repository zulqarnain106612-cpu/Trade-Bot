"""
DATA-003 — one clock policy, enforced at the boundary.

The failure modes this exists to close are all quiet ones:

- A naive datetime compared against another naive datetime succeeds and
  silently means local time.
- A naive datetime compared against an aware one raises, in production, on
  whichever path happened not to be exercised.
- A local-zone timestamp survives a DST shift by moving an hour, and nothing
  reports an error because nothing was wrong with the arithmetic.
- A host whose NTP has stopped signs an order the venue then rejects, and the
  rejection says nothing about clocks.

The policy is therefore: aware UTC or integer epoch milliseconds, nothing
else; naive is refused rather than assumed; and venue-versus-local skew is
measured against a declared budget.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.data.clock import (
    DEFAULT_SKEW_BUDGET_MS,
    ClockError,
    check_exchange_skew,
    ensure_utc,
    from_epoch_ms,
    to_epoch_ms,
    utc_now,
    utc_now_ms,
)


class TestNowIsAlwaysAware:
    def test_utc_now_is_aware_and_utc(self):
        now = utc_now()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0)

    def test_utc_now_ms_agrees_with_utc_now(self):
        assert abs(utc_now_ms() - int(utc_now().timestamp() * 1000)) < 1_000


class TestNaiveIsRefusedNotAssumed:
    def test_a_naive_datetime_raises(self):
        with pytest.raises(ClockError, match="naive datetime refused"):
            ensure_utc(datetime(2026, 9, 11, 12, 0, 0))

    def test_the_message_says_what_to_do(self):
        with pytest.raises(ClockError, match="attach a timezone at the boundary"):
            ensure_utc(datetime(2026, 9, 11))

    @pytest.mark.parametrize("value", ["2026-09-11", 1_757_592_000_000, None, 3.5])
    def test_a_non_datetime_raises(self, value):
        with pytest.raises(ClockError, match="expected a datetime"):
            ensure_utc(value)

    def test_an_aware_utc_datetime_passes_through(self):
        moment = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
        assert ensure_utc(moment) == moment

    def test_another_zone_is_converted_not_relabelled(self):
        # The distinction that matters: converting preserves the instant,
        # relabelling moves it. 08:00 in New York is 12:00 UTC.
        eastern = datetime(2026, 9, 11, 8, 0, 0, tzinfo=ZoneInfo("America/New_York"))
        assert ensure_utc(eastern) == datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)

    def test_a_fixed_offset_is_converted(self):
        offset = datetime(2026, 9, 11, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        assert ensure_utc(offset) == datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)


class TestThereIsNoDaylightSaving:
    def test_the_spring_forward_hour_does_not_move_a_utc_instant(self):
        # 2026-03-08 07:00 UTC is 02:00 EST, an hour that does not exist in
        # New York local time. Storing UTC means the bar is unambiguous.
        before = datetime(2026, 3, 8, 6, 59, tzinfo=UTC)
        after = datetime(2026, 3, 8, 7, 1, tzinfo=UTC)
        assert (after - before) == timedelta(minutes=2)
        assert to_epoch_ms(after) - to_epoch_ms(before) == 120_000

    def test_the_autumn_repeated_hour_is_unambiguous_in_utc(self):
        # 01:30 EDT and 01:30 EST are the same wall clock and different
        # instants. In UTC they are simply 05:30 and 06:30.
        first = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
        second = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
        assert to_epoch_ms(second) - to_epoch_ms(first) == 3_600_000


class TestEpochRoundTrip:
    @pytest.mark.parametrize("ms", [0, 1, 1_757_592_000_000, 1_757_592_000_123, 2_000_000_000_000])
    def test_ms_to_datetime_and_back(self, ms):
        assert to_epoch_ms(from_epoch_ms(ms)) == ms

    def test_a_float_epoch_keeps_its_sub_millisecond_part(self):
        # ccxt hands back floats. Truncating to whole milliseconds here would
        # be a silent lossy conversion, so the fractional part survives into
        # the microsecond field and to_epoch_ms drops it explicitly.
        moment = from_epoch_ms(1_757_592_000_000.5)
        assert moment.microsecond == 500
        assert to_epoch_ms(moment) == 1_757_592_000_000

    @pytest.mark.parametrize("bad", [1e30, -1e30, math.nan, math.inf])
    def test_an_out_of_range_epoch_raises(self, bad):
        with pytest.raises(ClockError, match="out of range"):
            from_epoch_ms(bad)

    def test_the_epoch_itself_is_the_unix_epoch(self):
        assert from_epoch_ms(0) == datetime(1970, 1, 1, tzinfo=UTC)

    def test_to_epoch_ms_refuses_a_naive_datetime(self):
        with pytest.raises(ClockError, match="naive"):
            to_epoch_ms(datetime(2026, 9, 11))


class TestExchangeSkew:
    def test_a_synchronised_clock_is_within_budget(self):
        report = check_exchange_skew(1_757_592_000_000, 1_757_592_000_000)
        assert report.skew_ms == 0
        assert report.within_budget
        assert report.reason == ""

    def test_a_venue_ahead_of_us(self):
        report = check_exchange_skew(1_757_592_030_000, 1_757_592_000_000)
        assert report.skew_ms == 30_000
        assert not report.within_budget
        assert "ahead of" in report.reason

    def test_a_venue_behind_us(self):
        report = check_exchange_skew(1_757_591_970_000, 1_757_592_000_000)
        assert report.skew_ms == -30_000
        assert not report.within_budget
        assert "behind" in report.reason

    def test_exactly_on_the_budget_is_within_it(self):
        report = check_exchange_skew(1_757_592_000_000 + DEFAULT_SKEW_BUDGET_MS, 1_757_592_000_000)
        assert report.within_budget

    def test_one_millisecond_past_the_budget_is_not(self):
        report = check_exchange_skew(
            1_757_592_000_000 + DEFAULT_SKEW_BUDGET_MS + 1, 1_757_592_000_000
        )
        assert not report.within_budget

    def test_the_budget_is_configurable(self):
        assert check_exchange_skew(1_000_100, 1_000_000, budget_ms=50).within_budget is False
        assert check_exchange_skew(1_000_100, 1_000_000, budget_ms=200).within_budget is True

    def test_the_local_side_defaults_to_now(self):
        # Not an assertion about the exact value -- an assertion that omitting
        # the local side does not silently compare against zero.
        report = check_exchange_skew(utc_now_ms())
        assert abs(report.skew_ms) < 1_000

    def test_the_reason_explains_the_consequence(self):
        report = check_exchange_skew(1_757_592_060_000, 1_757_592_000_000)
        assert "NTP" in report.reason
        assert "rejected by the venue" in report.reason
