"""Tests for the v6 strategy promotion gauntlet."""

from __future__ import annotations

from src.tuning.promotion_gauntlet import (
    GauntletCriteria,
    GauntletObservation,
    evaluate_gauntlet,
)


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
