"""Tests for the v6 strategy promotion gauntlet."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.diagnostics.attribution import AttributedFill
from src.tuning.promotion_gauntlet import (
    GauntletCriteria,
    GauntletObservation,
    evaluate_gauntlet,
    observation_from_fills,
)


_DAY_MS = 86_400_000


def test_passes_when_all_criteria_met() -> None:
    obs = GauntletObservation(
        trade_count=50, days_running=30, realized_sharpe=1.2, realized_max_drawdown_pct=0.10
    )
    result = evaluate_gauntlet(obs)
    assert result.passed
    assert result.failed_criteria == ()


def test_fails_on_insufficient_trades() -> None:
    obs = GauntletObservation(
        trade_count=5, days_running=30, realized_sharpe=1.2, realized_max_drawdown_pct=0.10
    )
    result = evaluate_gauntlet(obs)
    assert not result.passed
    assert any("trade_count" in f for f in result.failed_criteria)


def test_fails_on_insufficient_days() -> None:
    obs = GauntletObservation(
        trade_count=50, days_running=3, realized_sharpe=1.2, realized_max_drawdown_pct=0.10
    )
    result = evaluate_gauntlet(obs)
    assert not result.passed
    assert any("days_running" in f for f in result.failed_criteria)


def test_fails_on_low_sharpe() -> None:
    obs = GauntletObservation(
        trade_count=50, days_running=30, realized_sharpe=0.1, realized_max_drawdown_pct=0.10
    )
    result = evaluate_gauntlet(obs)
    assert not result.passed
    assert any("realized_sharpe" in f for f in result.failed_criteria)


def test_fails_on_excessive_drawdown() -> None:
    obs = GauntletObservation(
        trade_count=50, days_running=30, realized_sharpe=1.2, realized_max_drawdown_pct=0.50
    )
    result = evaluate_gauntlet(obs)
    assert not result.passed
    assert any("drawdown" in f for f in result.failed_criteria)


def test_reports_all_failed_criteria_simultaneously() -> None:
    obs = GauntletObservation(
        trade_count=1, days_running=1, realized_sharpe=-1.0, realized_max_drawdown_pct=0.9
    )
    result = evaluate_gauntlet(obs)
    assert not result.passed
    assert len(result.failed_criteria) == 4


def test_custom_criteria_respected() -> None:
    obs = GauntletObservation(
        trade_count=10, days_running=5, realized_sharpe=0.3, realized_max_drawdown_pct=0.25
    )
    lenient = GauntletCriteria(
        min_trades=5, min_days_running=3, min_sharpe=0.1, max_drawdown_pct=0.30
    )
    result = evaluate_gauntlet(obs, lenient)
    assert result.passed


# ---------------------------------------------------------------------------
# observation_from_fills — attribution -> gauntlet adapter
# ---------------------------------------------------------------------------


def _fill(pnl: float, entry_day: int, strategy_id: str = "alpha") -> AttributedFill:
    return AttributedFill(
        strategy_id=strategy_id,
        pnl_usd=pnl,
        entry_ts=entry_day * _DAY_MS,
        exit_ts=(entry_day + 1) * _DAY_MS,
    )


def test_observation_counts_only_the_named_strategy() -> None:
    fills = [_fill(10.0, 1), _fill(20.0, 2), _fill(-5.0, 3, strategy_id="beta")]
    obs = observation_from_fills("alpha", fills, equity_usd=10_000.0, now_ms=10 * _DAY_MS)
    assert obs.trade_count == 2


def test_days_running_measured_from_first_entry_to_now() -> None:
    """Not to the last exit — a candidate that went quiet has still been running."""
    fills = [_fill(10.0, 1), _fill(10.0, 2)]
    obs = observation_from_fills("alpha", fills, equity_usd=10_000.0, now_ms=31 * _DAY_MS)
    assert obs.days_running == 30.0


def test_no_fills_yields_zero_observation() -> None:
    obs = observation_from_fills("alpha", [], equity_usd=10_000.0, now_ms=10 * _DAY_MS)
    assert obs.trade_count == 0
    assert obs.days_running == 0.0
    assert obs.realized_sharpe == 0.0
    assert obs.realized_max_drawdown_pct == 0.0


def test_fills_only_for_other_strategies_yields_zero_observation() -> None:
    obs = observation_from_fills(
        "alpha", [_fill(10.0, 1, strategy_id="beta")], equity_usd=10_000.0, now_ms=10 * _DAY_MS
    )
    assert obs.trade_count == 0
    assert obs.days_running == 0.0


def test_drawdown_converted_to_fraction_of_equity() -> None:
    # peak equity +100 then -40 -> 40 USD drawdown against 1000 USD book = 4%
    fills = [_fill(100.0, 1), _fill(-40.0, 2)]
    obs = observation_from_fills("alpha", fills, equity_usd=1_000.0, now_ms=10 * _DAY_MS)
    assert obs.realized_max_drawdown_pct == pytest.approx(0.04)


def test_non_positive_equity_fails_closed_at_full_drawdown() -> None:
    """An unmeasurable denominator must not read as a flattering 0% drawdown."""
    fills = [_fill(100.0, 1), _fill(-40.0, 2)]
    obs = observation_from_fills("alpha", fills, equity_usd=0.0, now_ms=10 * _DAY_MS)
    assert obs.realized_max_drawdown_pct == 1.0
    assert not evaluate_gauntlet(obs).passed


def test_clock_skew_cannot_produce_negative_days_running() -> None:
    fills = [_fill(10.0, 5)]
    obs = observation_from_fills("alpha", fills, equity_usd=10_000.0, now_ms=1 * _DAY_MS)
    assert obs.days_running == 0.0


def test_adapter_output_feeds_evaluate_gauntlet() -> None:
    fills = [_fill(10.0, day) for day in range(40)]
    obs = observation_from_fills("alpha", fills, equity_usd=10_000.0, now_ms=60 * _DAY_MS)
    result = evaluate_gauntlet(obs, GauntletCriteria(min_sharpe=0.0))
    assert result.passed, result.failed_criteria


def test_default_now_ms_uses_wall_clock() -> None:
    now = int(datetime.now(tz=UTC).timestamp() * 1000)
    fills = [AttributedFill(strategy_id="alpha", pnl_usd=1.0, entry_ts=now - _DAY_MS, exit_ts=now)]
    obs = observation_from_fills("alpha", fills, equity_usd=10_000.0)
    assert 0.9 < obs.days_running < 1.1
