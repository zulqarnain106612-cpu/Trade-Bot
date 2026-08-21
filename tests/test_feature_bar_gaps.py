"""
Gapped OHLCV must be reported, because every window in the pipeline counts bars.

Nothing in the feature path checked that bars were contiguous in time. The
rolling windows -- volatility, ATR, Sharpe, volume z-score -- are all
bar-counted, so a gapped series silently changes what they measure: a 20-bar
volatility estimate can span days instead of hours, and close.shift(1)
charges a multi-hour jump as a single bar's return, inflating vol and every
size scalar derived from it.

Gaps are reported, never repaired. Filling them would fabricate prices that
never traded, and CLAUDE.md is explicit that OHLCV gaps are real rather than
artifacts. This is the same posture as the existing flat-price check.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.features.pipeline import bar_gap_report


_HOUR = 3_600_000


def _index(offsets_hours: list[int]) -> pd.Index:
    base = 1_700_000_000_000
    return pd.Index([base + h * _HOUR for h in offsets_hours])


def test_a_contiguous_index_reports_no_gaps() -> None:
    r = bar_gap_report(_index(list(range(50))))

    assert r["gap_count"] == 0
    assert r["missing_bars"] == 0
    assert r["expected_ms"] == _HOUR


def test_a_single_missing_bar_is_detected() -> None:
    hours = [h for h in range(50) if h != 20]
    r = bar_gap_report(_index(hours))

    assert r["gap_count"] == 1
    assert r["missing_bars"] == 1
    assert r["max_gap_ms"] == 2 * _HOUR


def test_a_multi_bar_outage_counts_every_missing_bar() -> None:
    hours = [h for h in range(50) if not 20 <= h < 26]  # six bars gone
    r = bar_gap_report(_index(hours))

    assert r["gap_count"] == 1
    assert r["missing_bars"] == 6
    assert r["max_gap_ms"] == 7 * _HOUR


def test_separate_outages_are_counted_separately() -> None:
    hours = [h for h in range(50) if h not in (10, 30, 31)]
    r = bar_gap_report(_index(hours))

    assert r["gap_count"] == 2
    assert r["missing_bars"] == 3


def test_the_expected_spacing_is_inferred_not_assumed() -> None:
    # A 5-minute series must not be judged against an hourly expectation.
    base = 1_700_000_000_000
    five_min = 300_000
    idx = pd.Index([base + i * five_min for i in range(40)])

    assert bar_gap_report(idx)["expected_ms"] == five_min
    assert bar_gap_report(idx)["gap_count"] == 0


def test_timestamp_jitter_below_the_tolerance_is_not_a_gap() -> None:
    # Exchanges do not always stamp bars to the millisecond; a small drift
    # must not be reported as missing data.
    base = 1_700_000_000_000
    idx = pd.Index([base + i * _HOUR + (i % 3) * 900 for i in range(40)])

    assert bar_gap_report(idx)["gap_count"] == 0


def test_gap_pct_is_relative_to_the_number_of_intervals() -> None:
    hours = [h for h in range(21) if h != 10]  # 20 points, 19 intervals
    r = bar_gap_report(_index(hours))

    assert r["gap_count"] == 1
    assert r["gap_pct"] == pytest.approx(100.0 / 19, abs=1e-3)


def test_too_short_an_index_has_no_inferable_spacing() -> None:
    for n in (0, 1, 2):
        r = bar_gap_report(_index(list(range(n))))
        assert r["gap_count"] == 0
        assert r["expected_ms"] == 0


def test_a_duplicated_timestamp_does_not_crash_the_report() -> None:
    # Zero-diff rows can appear when two pages of pagination overlap.
    idx = pd.Index([1_700_000_000_000 + h * _HOUR for h in [0, 1, 1, 2, 3, 4, 5]])
    r = bar_gap_report(idx)

    assert r["gap_count"] == 0
    assert r["expected_ms"] == _HOUR
