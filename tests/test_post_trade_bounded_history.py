"""
Tests for PostTradeAnalytics memory bounds and all-time aggregates.

_fill_history grew one FillRecord per fill and nothing ever trimmed it, in a
process designed to run for months. The fix has to bound it without making
algo_breakdown() quietly become "the last N fills" — a reporting surface that
silently narrows its own window is worse than one that grows.
"""

from __future__ import annotations

import pytest

from src.execution.post_trade import (
    _MAX_FILL_HISTORY,
    AlgoStats,
    FillRecord,
    PostTradeAnalytics,
)


def _fill(algo: str = "ioc", *, eq: float = 0.5, slip: float = 2.0) -> FillRecord:
    return FillRecord(
        symbol="BTC/USDT",
        venue="binance",
        algo=algo,
        side="buy",
        horizon_idx=0,
        requested_qty=1.0,
        filled_qty=1.0,
        fill_ratio=1.0,
        avg_fill_price=100.0,
        signal_price=100.0,
        slippage_bps=slip,
        fee_usd=0.1,
        pnl_usd=0.0,
        execution_quality_score=eq,
        ts=None,
        error=None,
    )


def _drive(analytics: PostTradeAnalytics, fills: list[FillRecord]) -> None:
    """Apply fills the way record() does, without a RouteResult."""
    for f in fills:
        analytics._algo_stats.setdefault(f.algo, AlgoStats(algo=f.algo)).update(f)
        analytics._fill_history.append(f)


# ------------------------------------------------------------------ bounds


def test_raw_history_is_bounded() -> None:
    analytics = PostTradeAnalytics()
    _drive(analytics, [_fill() for _ in range(_MAX_FILL_HISTORY + 2_000)])
    assert len(analytics._fill_history) == _MAX_FILL_HISTORY


def test_the_retained_records_are_the_most_recent() -> None:
    analytics = PostTradeAnalytics()
    _drive(analytics, [_fill(eq=float(i % 7)) for i in range(_MAX_FILL_HISTORY + 5)])
    # The last fill written must survive; the first must not.
    assert analytics.recent_fills(1)[0].execution_quality_score == pytest.approx(
        float((_MAX_FILL_HISTORY + 4) % 7)
    )


def test_recent_fills_returns_oldest_first_within_the_window() -> None:
    analytics = PostTradeAnalytics()
    _drive(analytics, [_fill(eq=float(i)) for i in range(5)])
    scores = [f.execution_quality_score for f in analytics.recent_fills(3)]
    assert scores == [2.0, 3.0, 4.0]


def test_execution_quality_trend_reads_the_tail() -> None:
    analytics = PostTradeAnalytics()
    _drive(analytics, [_fill(eq=0.0) for _ in range(50)])
    _drive(analytics, [_fill(eq=1.0) for _ in range(50)])
    assert analytics.execution_quality_trend(window=50) == pytest.approx(1.0)


def test_trend_on_an_empty_history_is_zero_not_a_division_error() -> None:
    assert PostTradeAnalytics().execution_quality_trend() == 0.0


# -------------------------------------------------------- all-time accuracy


def test_algo_breakdown_covers_every_fill_not_just_the_retained_window() -> None:
    # The whole point of the running aggregates: bounding memory must not
    # silently narrow what this reports.
    analytics = PostTradeAnalytics()
    total = _MAX_FILL_HISTORY + 7_000
    _drive(analytics, [_fill("ioc" if i % 2 else "twap") for i in range(total)])

    breakdown = analytics.algo_breakdown()
    assert sum(int(v["count"]) for v in breakdown.values()) == total
    assert int(breakdown["ioc"]["count"]) == total // 2
    assert len(analytics._fill_history) == _MAX_FILL_HISTORY


def test_algo_breakdown_averages_are_exact_over_all_time() -> None:
    analytics = PostTradeAnalytics()
    _drive(analytics, [_fill("ioc", slip=1.0) for _ in range(_MAX_FILL_HISTORY)])
    _drive(analytics, [_fill("ioc", slip=3.0) for _ in range(_MAX_FILL_HISTORY)])
    # Half at 1.0 and half at 3.0 -> 2.0, even though only the 3.0 half is
    # still retained as records.
    assert analytics.algo_breakdown()["ioc"]["avg_slippage_bps"] == pytest.approx(2.0)


def test_algo_breakdown_is_empty_before_any_fill() -> None:
    assert PostTradeAnalytics().algo_breakdown() == {}


def test_algo_stats_averages_do_not_divide_by_zero() -> None:
    assert AlgoStats(algo="ioc").as_dict()["avg_fill_ratio"] == 0.0
