"""Tests for per-strategy P&L attribution (v2 Sub-task 4)."""

from __future__ import annotations

import pytest

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
    assert attr.sortino == 0.0
    assert attr.calmar == 0.0
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


def test_sortino_single_trade_returns_zero() -> None:
    fills = [AttributedFill("s", 5.0, 1, 2)]
    attr = compute_attribution("s", fills)
    assert attr.sortino == 0.0


def test_sortino_no_losses_returns_sharpe() -> None:
    """When there are no losses sortino falls back to Sharpe value."""
    fills = [
        AttributedFill("s", 10.0, 1, 2),
        AttributedFill("s", 20.0, 2, 3),
    ]
    attr = compute_attribution("s", fills)
    assert attr.sortino == attr.sharpe


def test_sortino_penalizes_downside_more_than_sharpe() -> None:
    """A mix of gains and losses: Sortino uses only downside in denominator."""
    fills = [
        AttributedFill("s", 10.0, 1, 2),
        AttributedFill("s", -5.0, 2, 3),
        AttributedFill("s", 8.0, 3, 4),
        AttributedFill("s", -2.0, 4, 5),
    ]
    attr = compute_attribution("s", fills)
    # Sortino should differ from Sharpe in this mixed case
    assert attr.sortino != attr.sharpe
    assert attr.sortino > 0  # net positive P&L → positive ratio


def test_calmar_zero_when_no_drawdown() -> None:
    """All wins, no drawdown → Calmar = 0 (undefined; we return 0)."""
    fills = [
        AttributedFill("s", 5.0, 1, 2),
        AttributedFill("s", 5.0, 2, 3),
    ]
    attr = compute_attribution("s", fills)
    assert attr.calmar == 0.0


def test_calmar_positive_with_drawdown() -> None:
    fills = [
        AttributedFill("s", 100.0, 1, 2),
        AttributedFill("s", -20.0, 2, 3),
        AttributedFill("s", 50.0, 3, 4),
    ]
    attr = compute_attribution("s", fills)
    # max_dd = 20, total_pnl = 130, calmar = 130/20 = 6.5
    assert attr.calmar > 0.0
    assert abs(attr.calmar - 6.5) < 1e-6


def test_to_dict_includes_sortino_and_calmar() -> None:
    fills = [
        AttributedFill("s", 10.0, 1, 2),
        AttributedFill("s", -5.0, 2, 3),
    ]
    d = compute_attribution("s", fills).to_dict()
    assert "sortino" in d
    assert "calmar" in d


def test_fills_for_returns_only_that_strategy_in_record_order() -> None:
    tracker = AttributionTracker()
    a1 = AttributedFill("alpha", 10.0, 1, 2)
    b1 = AttributedFill("beta", -3.0, 1, 2)
    a2 = AttributedFill("alpha", 5.0, 3, 4)
    for fill in (a1, b1, a2):
        tracker.record(fill)

    assert tracker.fills_for("alpha") == [a1, a2]
    assert tracker.fills_for("beta") == [b1]


def test_fills_for_unknown_strategy_is_empty() -> None:
    tracker = AttributionTracker()
    tracker.record(AttributedFill("alpha", 10.0, 1, 2))
    assert tracker.fills_for("gamma") == []


# ---------------------------------------------------------------------------
# Bounded retention — memory and lifetime-scalar behaviour
# ---------------------------------------------------------------------------


def test_retained_window_is_bounded_per_strategy() -> None:
    tracker = AttributionTracker(max_fills_per_strategy=5)
    for i in range(20):
        tracker.record(AttributedFill("alpha", 1.0, i, i + 1))
    assert len(tracker.fills_for("alpha")) == 5


def test_eviction_keeps_the_most_recent_fills() -> None:
    tracker = AttributionTracker(max_fills_per_strategy=3)
    for i in range(6):
        tracker.record(AttributedFill("alpha", float(i), i, i + 1))
    assert [f.pnl_usd for f in tracker.fills_for("alpha")] == [3.0, 4.0, 5.0]


def test_lifetime_totals_survive_eviction() -> None:
    """Counts and P&L must not shrink when old fills age out of the window."""
    tracker = AttributionTracker(max_fills_per_strategy=2)
    for _ in range(10):
        tracker.record(AttributedFill("alpha", 3.0, 1, 2))
    attr = tracker.snapshot()["alpha"]
    assert attr.trade_count == 10
    assert attr.total_pnl_usd == pytest.approx(30.0)
    assert tracker.lifetime_trade_count("alpha") == 10
    assert tracker.fill_count() == 10


def test_lifetime_win_rate_survives_eviction() -> None:
    tracker = AttributionTracker(max_fills_per_strategy=2)
    for _ in range(5):
        tracker.record(AttributedFill("alpha", 1.0, 1, 2))
    for _ in range(5):
        tracker.record(AttributedFill("alpha", -1.0, 1, 2))
    assert tracker.snapshot()["alpha"].win_rate == pytest.approx(0.5)


def test_first_entry_ts_survives_eviction() -> None:
    """A long-lived strategy must not look newly started once its oldest fills age out."""
    tracker = AttributionTracker(max_fills_per_strategy=2)
    tracker.record(AttributedFill("alpha", 1.0, 1_000, 1_001))
    for i in range(5):
        tracker.record(AttributedFill("alpha", 1.0, 90_000 + i, 90_001 + i))
    assert tracker.first_entry_ts_for("alpha") == 1_000


def test_first_entry_ts_tracks_out_of_order_fills() -> None:
    tracker = AttributionTracker()
    tracker.record(AttributedFill("alpha", 1.0, 5_000, 5_001))
    tracker.record(AttributedFill("alpha", 1.0, 2_000, 2_001))
    assert tracker.first_entry_ts_for("alpha") == 2_000


def test_lifetime_accessors_on_unknown_strategy() -> None:
    tracker = AttributionTracker()
    assert tracker.first_entry_ts_for("nobody") is None
    assert tracker.lifetime_trade_count("nobody") == 0
    assert tracker.fills_for("nobody") == []


def test_strategies_are_isolated_from_each_others_windows() -> None:
    """One noisy strategy must not evict another's history."""
    tracker = AttributionTracker(max_fills_per_strategy=3)
    tracker.record(AttributedFill("beta", 7.0, 1, 2))
    for i in range(50):
        tracker.record(AttributedFill("alpha", 1.0, i, i + 1))
    assert [f.pnl_usd for f in tracker.fills_for("beta")] == [7.0]


def test_risk_ratios_are_window_scoped() -> None:
    """Sharpe/drawdown reflect recent behaviour, not a strategy's first month forever."""
    tracker = AttributionTracker(max_fills_per_strategy=4)
    for _ in range(10):
        tracker.record(AttributedFill("alpha", -50.0, 1, 2))
    for _ in range(4):
        tracker.record(AttributedFill("alpha", 10.0, 1, 2))
    attr = tracker.snapshot()["alpha"]
    assert attr.max_drawdown_usd == 0.0  # the loss run has aged out of the window
    assert attr.total_pnl_usd == pytest.approx(-460.0)  # but it still counts against lifetime P&L


def test_reset_clears_lifetime_state() -> None:
    tracker = AttributionTracker()
    tracker.record(AttributedFill("alpha", 1.0, 1, 2))
    tracker.reset()
    assert tracker.snapshot() == {}
    assert tracker.fill_count() == 0
    assert tracker.first_entry_ts_for("alpha") is None
