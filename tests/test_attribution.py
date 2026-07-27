"""Tests for per-strategy P&L attribution (v2 Sub-task 4)."""

from __future__ import annotations

from src.diagnostics.attribution import (
    AttributedFill,
    AttributionTracker,
    compute_attribution,
)


def test_compute_attribution_empty_returns_zeros() -> None:
    attr = compute_attribution("strat_a", [])
    assert attr.trade_count == 0
    assert attr.total_pnl_usd == 0.0
    assert attr.win_rate == 0.0
    assert attr.sharpe == 0.0
    assert attr.max_drawdown_usd == 0.0


def test_compute_attribution_filters_by_strategy_id() -> None:
    fills = [
        AttributedFill("strat_a", 10.0, 1, 2),
        AttributedFill("strat_b", -5.0, 1, 2),
        AttributedFill("strat_a", -3.0, 2, 3),
    ]
    attr = compute_attribution("strat_a", fills)
    assert attr.trade_count == 2
    assert attr.total_pnl_usd == 7.0


def test_win_rate_computed_correctly() -> None:
    fills = [
        AttributedFill("strat_a", 10.0, 1, 2),
        AttributedFill("strat_a", 10.0, 2, 3),
        AttributedFill("strat_a", -5.0, 3, 4),
        AttributedFill("strat_a", -5.0, 4, 5),
    ]
    attr = compute_attribution("strat_a", fills)
    assert attr.win_rate == 0.5


def test_max_drawdown_tracks_peak_to_trough() -> None:
    fills = [
        AttributedFill("strat_a", 100.0, 1, 2),
        AttributedFill("strat_a", -50.0, 2, 3),
        AttributedFill("strat_a", -30.0, 3, 4),
        AttributedFill("strat_a", 200.0, 4, 5),
    ]
    attr = compute_attribution("strat_a", fills)
    assert attr.max_drawdown_usd == 80.0


def test_sharpe_zero_with_single_trade() -> None:
    fills = [AttributedFill("strat_a", 10.0, 1, 2)]
    attr = compute_attribution("strat_a", fills)
    assert attr.sharpe == 0.0


def test_sharpe_zero_variance_returns_zero() -> None:
    fills = [
        AttributedFill("strat_a", 10.0, 1, 2),
        AttributedFill("strat_a", 10.0, 2, 3),
    ]
    attr = compute_attribution("strat_a", fills)
    assert attr.sharpe == 0.0


def test_to_dict_rounds_values() -> None:
    fills = [AttributedFill("strat_a", 10.123456, 1, 2)]
    attr = compute_attribution("strat_a", fills)
    d = attr.to_dict()
    assert d["strategy_id"] == "strat_a"
    assert d["total_pnl_usd"] == 10.1235


def test_tracker_record_and_snapshot() -> None:
    tracker = AttributionTracker()
    tracker.record(AttributedFill("strat_a", 10.0, 1, 2))
    tracker.record(AttributedFill("strat_b", -5.0, 1, 2))
    snapshot = tracker.snapshot()
    assert set(snapshot.keys()) == {"strat_a", "strat_b"}
    assert snapshot["strat_a"].total_pnl_usd == 10.0
    assert tracker.fill_count() == 2


def test_tracker_snapshot_empty() -> None:
    tracker = AttributionTracker()
    assert tracker.snapshot() == {}
    assert tracker.fill_count() == 0
