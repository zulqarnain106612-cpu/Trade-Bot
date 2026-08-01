"""
Wiring tests for the v10 self-updating decision log.

decision_log_writer.py could format and append a structural-change record,
but nothing ever produced one — every automated change to the strategy mix
went unrecorded. The kill switch is the first producer: auto-disable,
structural-decay flag, and explicit re-enable.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.risk.performance_drift import PerformanceBaseline


@pytest.fixture
def log_path(tmp_path: Path):
    """Point the writer at a temp file for the duration of a test."""
    path = tmp_path / "AUTOMATED_DECISION_LOG.md"
    settings = MagicMock()
    settings.storage.decision_log_path = path
    with patch("src.risk.strategy_kill_switch.get_settings", return_value=settings):
        yield path


def _manager(drifted: bool, reason: str = "sharpe below baseline"):
    """Kill-switch manager whose drift check returns a fixed verdict."""
    from src.risk.performance_drift import DriftDetected
    from src.risk.strategy_kill_switch import StrategyKillSwitchManager

    manager = StrategyKillSwitchManager()
    manager.register_strategy(
        "mean_reversion_v1",
        PerformanceBaseline(
            train_sharpe=1.8,
            oos_sharpe=1.5,
            train_accuracy=0.6,
            oos_accuracy=0.55,
            train_win_rate=0.55,
            max_drawdown_pct=10.0,
            trades_in_backtest=600,
        ),
    )
    state = manager._states["mean_reversion_v1"]
    state.detector = MagicMock()
    state.detector.check_drift.return_value = DriftDetected(
        drifted=drifted, reason=reason, metric="sharpe"
    )
    state.detector.current_rolling_sharpe.return_value = None
    return manager


class TestAutoDisable:
    def test_a_disable_is_recorded(self, log_path: Path) -> None:
        _manager(drifted=True).evaluate("mean_reversion_v1", now_ms=1_700_000_000_000)
        text = log_path.read_text(encoding="utf-8")
        assert "strategy_disabled" in text
        assert "mean_reversion_v1" in text
        assert "sharpe below baseline" in text

    def test_no_drift_records_nothing(self, log_path: Path) -> None:
        _manager(drifted=False).evaluate("mean_reversion_v1")
        assert not log_path.exists()

    def test_a_repeat_evaluation_does_not_re_log(self, log_path: Path) -> None:
        """
        evaluate() runs every tick and drift stays detected once tripped; the
        entry belongs to the transition, not to the state.
        """
        manager = _manager(drifted=True)
        manager.evaluate("mean_reversion_v1")
        manager.evaluate("mean_reversion_v1")
        manager.evaluate("mean_reversion_v1")
        assert log_path.read_text(encoding="utf-8").count("strategy_disabled") == 1

    def test_the_file_is_appended_to_not_rewritten(self, log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("# Decision Log\n\npre-existing content\n", encoding="utf-8")
        _manager(drifted=True).evaluate("mean_reversion_v1")
        text = log_path.read_text(encoding="utf-8")
        assert "pre-existing content" in text
        assert "strategy_disabled" in text


class TestReEnable:
    def test_a_re_enable_is_recorded_with_the_prior_reason(self, log_path: Path) -> None:
        manager = _manager(drifted=True, reason="win rate collapsed")
        manager.evaluate("mean_reversion_v1")
        # force=True: re_enable runs the v6 promotion gauntlet, and this
        # manager has no attributed track record for it to evaluate. The
        # override path is also the one that must still record an entry.
        manager.re_enable("mean_reversion_v1", force=True)
        text = log_path.read_text(encoding="utf-8")
        assert "strategy_re_enabled" in text
        # The reason it was pulled is the context an auditor needs most.
        assert "win rate collapsed" in text

    def test_re_enabling_a_never_disabled_strategy_still_records(self, log_path: Path) -> None:
        _manager(drifted=False).re_enable("mean_reversion_v1", force=True)
        assert "strategy_re_enabled" in log_path.read_text(encoding="utf-8")


class TestStructuralDecay:
    def _decayed_manager(self):
        manager = _manager(drifted=False)
        state = manager._states["mean_reversion_v1"]
        state.detector.current_rolling_sharpe.return_value = 0.1
        state.decay_detector = MagicMock()
        state.decay_detector.is_decayed = True
        state.decay_detector.cusum_statistic = 3.21
        return manager

    def test_a_decay_flag_is_recorded(self, log_path: Path) -> None:
        self._decayed_manager().evaluate("mean_reversion_v1")
        text = log_path.read_text(encoding="utf-8")
        assert "strategy_structural_decay" in text
        assert "3.21" in text

    def test_decay_is_recorded_once_not_every_tick(self, log_path: Path) -> None:
        """The CUSUM statistic stays above threshold once crossed."""
        manager = self._decayed_manager()
        for _ in range(5):
            manager.evaluate("mean_reversion_v1")
        assert log_path.read_text(encoding="utf-8").count("strategy_structural_decay") == 1


class TestFailureIsolation:
    def test_an_unwritable_log_does_not_stop_the_disable(self, tmp_path: Path) -> None:
        """
        Losing auditability is not a reason to leave capital with a drifting
        strategy.
        """
        settings = MagicMock()
        # A path whose parent cannot be created.
        settings.storage.decision_log_path = tmp_path / "not-a-dir" / "x" / "log.md"
        manager = _manager(drifted=True)
        with (
            patch("src.risk.strategy_kill_switch.get_settings", return_value=settings),
            patch(
                "src.risk.strategy_kill_switch.append_to_decision_log",
                side_effect=OSError("read-only filesystem"),
            ),
        ):
            manager.evaluate("mean_reversion_v1")
        assert manager.is_enabled("mean_reversion_v1") is False

    def test_a_settings_fault_does_not_stop_the_disable(self) -> None:
        manager = _manager(drifted=True)
        with patch(
            "src.risk.strategy_kill_switch.get_settings",
            side_effect=RuntimeError("config exploded"),
        ):
            manager.evaluate("mean_reversion_v1")
        assert manager.is_enabled("mean_reversion_v1") is False
