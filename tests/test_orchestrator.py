"""Tests for src/engine/orchestrator.py — executor routing safety invariant.

Spec (README): only primary_timeframe trades real money; scalping (1m) and
swing (4h) streams are paper-only regardless of the global trading mode.
Orchestrator._executor_for() is what enforces that at runtime.
"""

from unittest.mock import MagicMock

import pytest

from src.config import Timeframe, invalidate_settings_cache
from src.engine.orchestrator import Orchestrator
from src.execution.live import LiveExecutor
from src.execution.paper import PaperExecutor


@pytest.fixture(autouse=True)
def reset_settings():
    invalidate_settings_cache()
    yield
    invalidate_settings_cache()


def _make_orchestrator() -> Orchestrator:
    return Orchestrator(storage=MagicMock(), fetcher=MagicMock())


class TestExecutorFor:
    def test_primary_timeframe_uses_live_executor(self):
        orch = _make_orchestrator()
        live_executor = MagicMock(spec=LiveExecutor)
        paper_executor = MagicMock(spec=PaperExecutor)
        orch._executor = live_executor
        orch._non_primary_executor = paper_executor
        orch._primary_tf = Timeframe.INTRADAY

        assert orch._executor_for(Timeframe.INTRADAY) is live_executor

    def test_non_primary_timeframe_never_uses_live_executor(self):
        orch = _make_orchestrator()
        live_executor = MagicMock(spec=LiveExecutor)
        paper_executor = MagicMock(spec=PaperExecutor)
        orch._executor = live_executor
        orch._non_primary_executor = paper_executor
        orch._primary_tf = Timeframe.INTRADAY

        assert orch._executor_for(Timeframe.SCALPING) is paper_executor
        assert orch._executor_for(Timeframe.SWING) is paper_executor

    def test_paper_mode_all_timeframes_share_single_executor(self):
        # trading_mode=PAPER never creates a _non_primary_executor — everyone
        # shares the single self._executor.
        orch = _make_orchestrator()
        paper_executor = MagicMock(spec=PaperExecutor)
        orch._executor = paper_executor
        orch._non_primary_executor = None
        orch._primary_tf = Timeframe.INTRADAY

        assert orch._executor_for(Timeframe.SCALPING) is paper_executor
        assert orch._executor_for(Timeframe.INTRADAY) is paper_executor
        assert orch._executor_for(Timeframe.SWING) is paper_executor
