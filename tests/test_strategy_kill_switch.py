"""Tests for the per-strategy kill-switch manager (v2 Sub-task 3)."""

from __future__ import annotations

import pytest

from src.risk.performance_drift import PerformanceBaseline
from src.risk.strategy_kill_switch import (
    StrategyKillSwitchManager,
    get_strategy_kill_switch_manager,
)


def _baseline(oos_sharpe: float = 2.0) -> PerformanceBaseline:
    return PerformanceBaseline(
        train_sharpe=2.5,
        oos_sharpe=oos_sharpe,
        train_accuracy=0.58,
        oos_accuracy=0.55,
        train_win_rate=0.54,
        max_drawdown_pct=0.10,
        trades_in_backtest=500,
    )


def test_register_and_enabled_by_default() -> None:
    mgr = StrategyKillSwitchManager()
    mgr.register_strategy("strat_a", _baseline())
    assert mgr.is_enabled("strat_a")


def test_double_registration_rejected() -> None:
    mgr = StrategyKillSwitchManager()
    mgr.register_strategy("strat_a", _baseline())
    with pytest.raises(ValueError, match="already"):
        mgr.register_strategy("strat_a", _baseline())


def test_unregistered_strategy_raises_keyerror() -> None:
    mgr = StrategyKillSwitchManager()
    with pytest.raises(KeyError):
        mgr.is_enabled("unknown")


def test_evaluate_disables_on_sharpe_drift() -> None:
    mgr = StrategyKillSwitchManager()
    mgr.register_strategy("strat_a", _baseline(oos_sharpe=3.0))

    for _ in range(40):
        mgr.record_trade_outcome(
            "strat_a",
            pnl_usd=-10.0,
            predicted_prob=0.4,
            actual_direction=1,
            current_equity=9000.0,
            starting_equity=10000.0,
        )

    drift = mgr.evaluate("strat_a")
    assert drift.drifted
    assert not mgr.is_enabled("strat_a")
    assert mgr.disabled_reason("strat_a") != ""


def test_evaluate_keeps_enabled_with_insufficient_trades() -> None:
    mgr = StrategyKillSwitchManager()
    mgr.register_strategy("strat_a", _baseline())
    drift = mgr.evaluate("strat_a")
    assert not drift.drifted
    assert mgr.is_enabled("strat_a")


def test_re_enable_clears_disabled_state() -> None:
    mgr = StrategyKillSwitchManager()
    mgr.register_strategy("strat_a", _baseline(oos_sharpe=3.0))
    for _ in range(40):
        mgr.record_trade_outcome(
            "strat_a",
            pnl_usd=-10.0,
            predicted_prob=0.4,
            actual_direction=1,
            current_equity=9000.0,
            starting_equity=10000.0,
        )
    mgr.evaluate("strat_a")
    assert not mgr.is_enabled("strat_a")

    mgr.re_enable("strat_a")
    assert mgr.is_enabled("strat_a")
    assert mgr.disabled_reason("strat_a") == ""


def test_evaluate_does_not_reenable_once_disabled() -> None:
    mgr = StrategyKillSwitchManager()
    mgr.register_strategy("strat_a", _baseline(oos_sharpe=3.0))
    for _ in range(40):
        mgr.record_trade_outcome(
            "strat_a",
            pnl_usd=-10.0,
            predicted_prob=0.4,
            actual_direction=1,
            current_equity=9000.0,
            starting_equity=10000.0,
        )
    mgr.evaluate("strat_a")
    assert not mgr.is_enabled("strat_a")

    for _ in range(10):
        mgr.record_trade_outcome(
            "strat_a",
            pnl_usd=100.0,
            predicted_prob=0.7,
            actual_direction=1,
            current_equity=9500.0,
            starting_equity=10000.0,
        )
    mgr.evaluate("strat_a")
    assert not mgr.is_enabled("strat_a")


def test_get_strategy_kill_switch_manager_singleton() -> None:
    m1 = get_strategy_kill_switch_manager()
    m2 = get_strategy_kill_switch_manager()
    assert m1 is m2


def test_strategies_are_independent() -> None:
    mgr = StrategyKillSwitchManager()
    mgr.register_strategy("strat_a", _baseline(oos_sharpe=3.0))
    mgr.register_strategy("strat_b", _baseline(oos_sharpe=3.0))

    for _ in range(40):
        mgr.record_trade_outcome(
            "strat_a",
            pnl_usd=-10.0,
            predicted_prob=0.4,
            actual_direction=1,
            current_equity=9000.0,
            starting_equity=10000.0,
        )
    mgr.evaluate("strat_a")

    assert not mgr.is_enabled("strat_a")
    assert mgr.is_enabled("strat_b")
