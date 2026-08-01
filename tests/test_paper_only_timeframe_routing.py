"""
Non-primary timeframes must never reach the live executor.

The README states that only the intraday stream trades real money;
scalping and swing are paper-only regardless of TRADING_MODE. That held,
but only incidentally: _executor_for fell back to self._executor whenever
the paper executor was unset, and in live mode that fallback is real money.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.config import Timeframe
from src.execution.live import LiveExecutor
from src.execution.paper import PaperExecutor


def _orch(executor, non_primary):
    from src.engine.orchestrator import Orchestrator

    orch = object.__new__(Orchestrator)
    orch._primary_tf = Timeframe.INTRADAY
    orch._executor = executor
    orch._non_primary_executor = non_primary
    orch._log = MagicMock()
    return orch


def _live():
    return MagicMock(spec=LiveExecutor)


def _paper():
    return MagicMock(spec=PaperExecutor)


class TestLiveMode:
    def test_the_primary_timeframe_reaches_the_live_executor(self) -> None:
        live = _live()
        assert _orch(live, _paper())._executor_for(Timeframe.INTRADAY) is live

    def test_a_non_primary_timeframe_is_routed_to_paper(self) -> None:
        paper = _paper()
        assert _orch(_live(), paper)._executor_for(Timeframe.SCALPING) is paper
        assert _orch(_live(), paper)._executor_for(Timeframe.SWING) is paper

    def test_a_missing_paper_executor_skips_rather_than_going_live(self) -> None:
        """
        The guard used to be `is not None`, so an unset paper executor fell
        through to real money. A paper-only stream that does not run costs a
        simulated trade; the alternative costs a real one.
        """
        orch = _orch(_live(), None)
        assert orch._executor_for(Timeframe.SCALPING) is None
        orch._log.error.assert_called_once()

    def test_the_primary_timeframe_is_unaffected_by_that_guard(self) -> None:
        live = _live()
        orch = _orch(live, None)
        assert orch._executor_for(Timeframe.INTRADAY) is live
        orch._log.error.assert_not_called()


class TestPaperMode:
    def test_every_timeframe_uses_the_single_paper_executor(self) -> None:
        """In paper mode there is no second executor and nothing to guard."""
        paper = _paper()
        orch = _orch(paper, None)
        for tf in (Timeframe.SCALPING, Timeframe.INTRADAY, Timeframe.SWING):
            assert orch._executor_for(tf) is paper
        orch._log.error.assert_not_called()


def test_startup_pairs_a_paper_executor_with_every_live_run() -> None:
    """
    The invariant the routing guard backstops: whenever trading_mode is LIVE
    and a non-primary timeframe is active, startup builds the paper executor.
    """
    import inspect

    from src.engine.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator.startup)
    assert "TradingMode.LIVE" in source
    assert "any(tf != self._primary_tf for tf in self._timeframes)" in source
    assert "self._non_primary_executor = PaperExecutor(" in source
