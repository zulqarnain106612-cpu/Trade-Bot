"""
Wiring tests for the v6 promotion gauntlet.

promotion_gauntlet.py evaluated a candidate's track record and nothing ever
called it. `re_enable()` documented that "callers are responsible for that
validation" — and had no callers at all, so a strategy auto-disabled for
drift could not be reinstated for the life of the process.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.diagnostics.attribution import AttributedFill, AttributionTracker
from src.risk.performance_drift import PerformanceBaseline
from src.risk.strategy_kill_switch import (
    GauntletNotPassedError,
    StrategyKillSwitchManager,
)
from src.tuning.promotion_gauntlet import GauntletCriteria, GauntletObservation


_DAY_MS = 86_400_000
_NOW = 1_700_000_000_000


def _baseline() -> PerformanceBaseline:
    return PerformanceBaseline(
        train_sharpe=1.8,
        oos_sharpe=1.5,
        train_accuracy=0.6,
        oos_accuracy=0.55,
        train_win_rate=0.55,
        max_drawdown_pct=10.0,
        trades_in_backtest=600,
    )


def _manager() -> StrategyKillSwitchManager:
    manager = StrategyKillSwitchManager()
    manager.register_strategy("strat_a", _baseline())
    manager._states["strat_a"].enabled = False
    manager._states["strat_a"].disabled_reason = "sharpe drift"
    return manager


def _tracker(pnls: list[float], *, days_ago: int = 30) -> AttributionTracker:
    tracker = AttributionTracker()
    entry = _NOW - days_ago * _DAY_MS
    for i, pnl in enumerate(pnls):
        tracker.record(
            AttributedFill(
                strategy_id="strat_a",
                pnl_usd=pnl,
                entry_ts=entry + i,
                exit_ts=entry + i + 1,
            )
        )
    return tracker


def _with_tracker(tracker: AttributionTracker):
    return patch(
        "src.risk.strategy_kill_switch.get_attribution_tracker",
        return_value=tracker,
    )


class TestObservationConstruction:
    def test_no_fills_gives_no_observation(self) -> None:
        """Absence of evidence, not evidence of a passing record."""
        with _with_tracker(AttributionTracker()):
            assert _manager().build_gauntlet_observation("strat_a") is None

    def test_days_running_measures_from_the_first_entry(self) -> None:
        with _with_tracker(_tracker([1.0] * 5, days_ago=21)):
            obs = _manager().build_gauntlet_observation("strat_a", now_ms=_NOW)
        assert obs is not None
        assert obs.days_running == pytest.approx(21.0, abs=0.01)

    def test_trade_count_comes_from_the_fills(self) -> None:
        with _with_tracker(_tracker([1.0] * 7)):
            obs = _manager().build_gauntlet_observation("strat_a", now_ms=_NOW)
        assert obs is not None
        assert obs.trade_count == 7

    def test_a_never_profitable_strategy_reports_full_drawdown(self) -> None:
        """
        No positive peak means no meaningful denominator. 1.0 fails the
        criterion, which is the right answer to "never made money, and we are
        being asked to give it capital again".
        """
        with _with_tracker(_tracker([-5.0] * 10)):
            obs = _manager().build_gauntlet_observation("strat_a", now_ms=_NOW)
        assert obs is not None
        assert obs.realized_max_drawdown_pct == pytest.approx(1.0)

    def test_drawdown_is_a_fraction_of_peak_cumulative_pnl(self) -> None:
        # Peak cumulative P&L 100, then a 20 drawdown -> 0.2.
        with _with_tracker(_tracker([100.0, -20.0, 5.0])):
            obs = _manager().build_gauntlet_observation("strat_a", now_ms=_NOW)
        assert obs is not None
        assert obs.realized_max_drawdown_pct == pytest.approx(0.2, abs=0.01)

    def test_only_this_strategys_fills_are_counted(self) -> None:
        tracker = _tracker([1.0] * 3)
        tracker.record(AttributedFill("other_v1", 999.0, _NOW, _NOW))
        with _with_tracker(tracker):
            obs = _manager().build_gauntlet_observation("strat_a", now_ms=_NOW)
        assert obs is not None
        assert obs.trade_count == 3


class TestReEnableEnforcement:
    _PASSING = GauntletObservation(
        trade_count=50,
        days_running=30.0,
        realized_sharpe=1.2,
        realized_max_drawdown_pct=0.05,
    )

    def test_a_passing_record_re_enables(self) -> None:
        manager = _manager()
        manager.re_enable("strat_a", observation=self._PASSING)
        assert manager.is_enabled("strat_a") is True
        assert manager.disabled_reason("strat_a") == ""

    def test_no_track_record_is_rejected(self) -> None:
        manager = _manager()
        with _with_tracker(AttributionTracker()), pytest.raises(GauntletNotPassedError):
            manager.re_enable("strat_a")
        assert manager.is_enabled("strat_a") is False

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("trade_count", 5),
            ("days_running", 2.0),
            ("realized_sharpe", -0.4),
            ("realized_max_drawdown_pct", 0.9),
        ],
    )
    def test_each_criterion_can_block_on_its_own(self, field: str, value: float) -> None:
        passing = {
            "trade_count": 50,
            "days_running": 30.0,
            "realized_sharpe": 1.2,
            "realized_max_drawdown_pct": 0.05,
        }
        failing = GauntletObservation(**{**passing, field: value})
        manager = _manager()
        with pytest.raises(GauntletNotPassedError) as exc:
            manager.re_enable("strat_a", observation=failing)
        assert exc.value.failed_criteria
        assert manager.is_enabled("strat_a") is False

    def test_a_failed_re_enable_never_partially_applies(self) -> None:
        manager = _manager()
        failing = GauntletObservation(
            trade_count=1, days_running=0.0, realized_sharpe=-2.0, realized_max_drawdown_pct=0.9
        )
        with pytest.raises(GauntletNotPassedError):
            manager.re_enable("strat_a", observation=failing)
        assert manager.is_enabled("strat_a") is False
        assert manager.disabled_reason("strat_a") == "sharpe drift"

    def test_force_overrides_a_failing_record(self) -> None:
        """A restart wipes the in-memory tracker, so an override must exist."""
        manager = _manager()
        manager.re_enable("strat_a", force=True)
        assert manager.is_enabled("strat_a") is True

    def test_force_does_not_consult_the_tracker_at_all(self) -> None:
        manager = _manager()
        tracker = MagicMock()
        with _with_tracker(tracker):
            manager.re_enable("strat_a", force=True)
        tracker.fills_for.assert_not_called()

    def test_custom_criteria_are_honoured(self) -> None:
        manager = _manager()
        lenient = GauntletCriteria(
            min_trades=1, min_days_running=0, min_sharpe=-10.0, max_drawdown_pct=1.0
        )
        weak = GauntletObservation(
            trade_count=1, days_running=0.0, realized_sharpe=-1.0, realized_max_drawdown_pct=0.9
        )
        manager.re_enable("strat_a", observation=weak, criteria=lenient)
        assert manager.is_enabled("strat_a") is True

    def test_the_observation_is_built_from_attribution_when_not_supplied(self) -> None:
        """A constant P&L series has zero dispersion, so the pnls alternate:
        _sharpe() returns 0.0 on zero std and min_sharpe would fail for the
        wrong reason."""
        manager = _manager()
        with _with_tracker(_tracker([10.0, 8.0] * 25, days_ago=30)):
            manager.re_enable("strat_a", now_ms=_NOW)
        assert manager.is_enabled("strat_a") is True
