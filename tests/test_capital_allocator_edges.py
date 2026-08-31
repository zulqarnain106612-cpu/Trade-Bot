"""Edge-case coverage for src/strategies/capital_allocator.py.

Targets the fallback branches the full-suite run listed as missing: the
zero-total cap path in _cap_and_renormalize, and the
attribution-unavailable fallbacks in both performance_weighted_allocate
and risk_parity_allocate.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.strategies.capital_allocator import (
    equal_weight_allocate,
    performance_weighted_allocate,
    risk_parity_allocate,
)


def _strategy(strategy_id: str, cap: float = 1.0) -> MagicMock:
    s = MagicMock()
    s.strategy_id = strategy_id
    s.required_capital_fraction.return_value = cap
    return s


def test_cap_and_renormalize_zero_caps_yield_all_zero_weights():
    # Every strategy caps at 0.0 -> capped total is 0 -> all-zero result
    # rather than a divide-by-zero.
    strategies = (_strategy("a", cap=0.0), _strategy("b", cap=0.0))
    result = equal_weight_allocate(strategies, {"a", "b"})
    assert result.fractions == {"a": 0.0, "b": 0.0}
    assert result.total() == 0.0


def test_equal_weight_no_active_strategies_returns_zeros():
    strategies = (_strategy("a"), _strategy("b"))
    result = equal_weight_allocate(strategies, set())
    assert result.fractions == {"a": 0.0, "b": 0.0}
    assert result.method == "equal_weight"


def test_equal_weight_splits_among_active_only():
    strategies = (_strategy("a"), _strategy("b"))
    result = equal_weight_allocate(strategies, {"a"})
    assert result.fractions["a"] == pytest.approx(1.0)
    assert result.fractions["b"] == 0.0


def test_performance_weighted_rejects_unknown_metric():
    strategies = (_strategy("a"),)
    with pytest.raises(ValueError, match="metric must be"):
        performance_weighted_allocate(strategies, {"a"}, metric="not_a_metric")


def test_performance_weighted_no_active_returns_zeros():
    strategies = (_strategy("a"),)
    result = performance_weighted_allocate(strategies, set())
    assert result.fractions == {"a": 0.0}
    assert result.method == "performance_weighted"


def test_performance_weighted_falls_back_when_attribution_unavailable():
    strategies = (_strategy("a"), _strategy("b"))
    with patch(
        "src.diagnostics.attribution.get_attribution_tracker",
        side_effect=RuntimeError("tracker down"),
    ):
        result = performance_weighted_allocate(strategies, {"a", "b"})
    # Falls back to equal weight, and says so in `method`.
    assert result.method == "equal_weight"
    assert result.fractions["a"] == pytest.approx(0.5)


def test_performance_weighted_warmup_gives_equal_share():
    strategies = (_strategy("a"), _strategy("b"))
    attr = MagicMock(trade_count=3, sortino=5.0, sharpe=5.0)
    tracker = MagicMock()
    tracker.snapshot.return_value = {"a": attr, "b": attr}
    with patch("src.diagnostics.attribution.get_attribution_tracker", return_value=tracker):
        result = performance_weighted_allocate(strategies, {"a", "b"})
    assert result.fractions["a"] == pytest.approx(0.5)


def test_performance_weighted_negative_score_is_floored_not_zeroed():
    strategies = (_strategy("a"), _strategy("b"))
    good = MagicMock(trade_count=100, sortino=2.0, sharpe=2.0)
    bad = MagicMock(trade_count=100, sortino=-1.0, sharpe=-1.0)
    tracker = MagicMock()
    tracker.snapshot.return_value = {"a": good, "b": bad}
    with patch("src.diagnostics.attribution.get_attribution_tracker", return_value=tracker):
        result = performance_weighted_allocate(strategies, {"a", "b"})
    # The drawdown strategy keeps a small allocation rather than being cut.
    assert result.fractions["b"] > 0.0
    assert result.fractions["a"] > result.fractions["b"]


def test_performance_weighted_all_zero_scores_falls_back_to_equal_weight():
    # Past warm-up but every score is exactly 0.0 -> total_raw is 0, so the
    # proportional split would divide by zero; falls back instead.
    strategies = (_strategy("a"), _strategy("b"))
    flat = MagicMock(trade_count=100, sortino=0.0, sharpe=0.0)
    tracker = MagicMock()
    tracker.snapshot.return_value = {"a": flat, "b": flat}
    with patch("src.diagnostics.attribution.get_attribution_tracker", return_value=tracker):
        result = performance_weighted_allocate(strategies, {"a", "b"})
    assert result.method == "equal_weight"
    assert result.fractions["a"] == pytest.approx(0.5)


def test_risk_parity_no_active_returns_zeros():
    strategies = (_strategy("a"),)
    result = risk_parity_allocate(strategies, set())
    assert result.fractions == {"a": 0.0}
    assert result.method == "risk_parity"


def test_risk_parity_falls_back_when_attribution_unavailable():
    strategies = (_strategy("a"), _strategy("b"))
    with patch(
        "src.diagnostics.attribution.get_attribution_tracker",
        side_effect=RuntimeError("tracker down"),
    ):
        result = risk_parity_allocate(strategies, {"a", "b"})
    assert result.method == "equal_weight"


def test_risk_parity_warmup_gives_equal_share():
    strategies = (_strategy("a"), _strategy("b"))
    attr = MagicMock(trade_count=2)
    tracker = MagicMock()
    tracker.snapshot.return_value = {"a": attr, "b": attr}
    tracker.fills_for.return_value = []
    with patch("src.diagnostics.attribution.get_attribution_tracker", return_value=tracker):
        result = risk_parity_allocate(strategies, {"a", "b"})
    assert result.fractions["a"] == pytest.approx(0.5)


def test_risk_parity_weights_inversely_to_volatility():
    strategies = (_strategy("calm"), _strategy("wild"))
    attr = MagicMock(trade_count=100)
    tracker = MagicMock()
    tracker.snapshot.return_value = {"calm": attr, "wild": attr}

    calm_fills = [MagicMock(pnl_usd=v) for v in [10.0, 11.0, 9.0, 10.0] * 10]
    wild_fills = [MagicMock(pnl_usd=v) for v in [500.0, -500.0, 300.0, -300.0] * 10]
    tracker.fills_for.side_effect = lambda sid: calm_fills if sid == "calm" else wild_fills

    with patch("src.diagnostics.attribution.get_attribution_tracker", return_value=tracker):
        result = risk_parity_allocate(strategies, {"calm", "wild"})

    assert result.fractions["calm"] > result.fractions["wild"]
