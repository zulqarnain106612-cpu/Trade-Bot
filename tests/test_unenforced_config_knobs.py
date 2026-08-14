"""
Tests for two config knobs that were declared and never read.

`SelfTuningSettings.min_trades_between_attempts` had no reader at all, so
only the wall-clock half of the tuning cadence guard was enforced.
`StorageSettings.bar_cache_days` configured a retention window that
`prune_old_bars()` implemented on both backends and nothing ever called.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import Timeframe, TradingMode
from src.tuning.audit import TuningAuditEntry, TuningEventType


def _runner(min_trades: int = 200, min_hours: float = 24.0):
    from src.tuning.runner import TuningRunner

    runner = object.__new__(TuningRunner)
    runner._settings = MagicMock(
        enabled=True,
        min_trades_between_attempts=min_trades,
        min_hours_between_attempts=min_hours,
    )
    runner._audit_log = MagicMock()
    return runner


def _proposed(hours_ago: float, details: dict) -> TuningAuditEntry:
    return TuningAuditEntry(
        param_name="hmm.entropy_threshold",
        event_type=TuningEventType.PROPOSED,
        timestamp=(datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(),
        details=details,
    )


class TestTradeCadenceGuard:
    def test_no_prior_attempt_is_never_in_cooldown(self) -> None:
        runner = _runner()
        runner._audit_log.read_for_param.return_value = []
        assert runner._cooldown_active("hmm.entropy_threshold", 1_000) == (False, "")

    def test_wall_clock_cooldown_still_blocks(self) -> None:
        runner = _runner()
        runner._audit_log.read_for_param.return_value = [_proposed(1.0, {"closed_trade_count": 0})]
        blocked, reason = runner._cooldown_active("hmm.entropy_threshold", 10_000)
        assert blocked is True
        assert reason == "cooldown_active"

    def test_enough_elapsed_time_but_too_few_new_trades_blocks(self) -> None:
        """
        A quiet market can let 24 hours pass on a handful of trades, and
        re-tuning on that little new evidence is how a tuner fits noise.
        """
        runner = _runner(min_trades=200)
        runner._audit_log.read_for_param.return_value = [
            _proposed(48.0, {"closed_trade_count": 1_000})
        ]
        blocked, reason = runner._cooldown_active("hmm.entropy_threshold", 1_050)
        assert blocked is True
        assert reason == "cooldown_active_insufficient_trades"

    def test_enough_time_and_enough_trades_clears(self) -> None:
        runner = _runner(min_trades=200)
        runner._audit_log.read_for_param.return_value = [
            _proposed(48.0, {"closed_trade_count": 1_000})
        ]
        assert runner._cooldown_active("hmm.entropy_threshold", 1_300) == (False, "")

    def test_an_unavailable_count_falls_back_to_wall_clock_only(self) -> None:
        """
        None means "this guard cannot claim a verdict", not "zero new trades"
        -- the latter would block tuning forever on a storage hiccup.
        """
        runner = _runner(min_trades=200)
        runner._audit_log.read_for_param.return_value = [
            _proposed(48.0, {"closed_trade_count": 1_000})
        ]
        assert runner._cooldown_active("hmm.entropy_threshold", None) == (False, "")

    def test_a_prior_attempt_without_the_field_cannot_be_measured_against(self) -> None:
        """Audit entries written before this field existed carry no baseline."""
        runner = _runner(min_trades=200)
        runner._audit_log.read_for_param.return_value = [_proposed(48.0, {})]
        assert runner._cooldown_active("hmm.entropy_threshold", 5) == (False, "")


class TestBarPruning:
    def _orch(self, keep_days: int = 90, timeframes=None):
        from src.engine.orchestrator import Orchestrator

        orch = object.__new__(Orchestrator)
        orch._symbol = "BTC/USDT"
        orch._timeframes = timeframes or [Timeframe.SCALPING, Timeframe.INTRADAY]
        orch._log = MagicMock()
        cfg = MagicMock()
        cfg.storage.bar_cache_days = keep_days
        cfg.trading_mode = TradingMode.PAPER
        orch._cfg = cfg
        orch._storage = MagicMock()
        orch._storage.prune_old_bars = AsyncMock(return_value=0)
        return orch

    @pytest.mark.asyncio
    async def test_every_active_timeframe_is_pruned(self) -> None:
        orch = self._orch()
        await orch._prune_old_bars()
        pruned = {c.args[1] for c in orch._storage.prune_old_bars.await_args_list}
        assert pruned == {Timeframe.SCALPING.value, Timeframe.INTRADAY.value}

    @pytest.mark.asyncio
    async def test_the_configured_retention_window_is_used(self) -> None:
        orch = self._orch(keep_days=30)
        await orch._prune_old_bars()
        assert orch._storage.prune_old_bars.await_args.args[2] == 30

    @pytest.mark.asyncio
    async def test_one_timeframe_failing_does_not_skip_the_rest(self) -> None:
        orch = self._orch()
        orch._storage.prune_old_bars = AsyncMock(side_effect=[RuntimeError("locked"), 5])
        await orch._prune_old_bars()
        assert orch._storage.prune_old_bars.await_count == 2

    @pytest.mark.asyncio
    async def test_a_pruning_failure_never_propagates(self) -> None:
        """Housekeeping must not take down an otherwise healthy tick loop."""
        orch = self._orch()
        orch._storage.prune_old_bars = AsyncMock(side_effect=RuntimeError("disk"))
        await orch._prune_old_bars()  # must not raise

    @pytest.mark.asyncio
    async def test_pruning_is_wired_to_the_tick_loop(self) -> None:
        """The gap being fixed was an implemented pruner with no caller."""
        import inspect

        from src.engine.orchestrator import Orchestrator

        assert "self._prune_old_bars()" in inspect.getsource(Orchestrator._tick)


class TestSchedulerSuppliesTheCount:
    @pytest.mark.asyncio
    async def test_a_storage_failure_yields_none_not_zero(self) -> None:
        from src.tuning.scheduler import AutoTuningScheduler

        scheduler = object.__new__(AutoTuningScheduler)
        scheduler._symbol = "BTC/USDT"
        scheduler._settings = MagicMock(trading_mode=TradingMode.PAPER)
        scheduler._storage = MagicMock()
        scheduler._storage.fetch_trades = AsyncMock(side_effect=RuntimeError("db down"))
        assert await scheduler._closed_trade_count() is None

    @pytest.mark.asyncio
    async def test_the_count_is_the_number_of_closed_trades(self) -> None:
        from src.tuning.scheduler import AutoTuningScheduler

        scheduler = object.__new__(AutoTuningScheduler)
        scheduler._symbol = "BTC/USDT"
        scheduler._settings = MagicMock(trading_mode=TradingMode.PAPER)
        scheduler._storage = MagicMock()
        scheduler._storage.fetch_trades = AsyncMock(return_value=[object()] * 7)
        assert await scheduler._closed_trade_count() == 7

    def test_trade_driven_groups_pass_the_count(self) -> None:
        """
        Bar-driven feature-window tuning deliberately does not; gating it on
        trade flow would stall it through any quiet period.
        """
        import inspect

        from src.tuning.scheduler import AutoTuningScheduler

        source = inspect.getsource(AutoTuningScheduler._attempt_all)
        assert source.count("closed_trade_count=closed_trade_count") == 2
        assert "deliberately NOT passed here" in source


@pytest.mark.asyncio
async def test_the_proposed_audit_entry_records_the_count() -> None:
    """Without this the next attempt has no baseline to measure against."""
    from src.tuning.runner import TuningRunner

    runner = object.__new__(TuningRunner)
    runner._settings = MagicMock(
        enabled=True, min_trades_between_attempts=200, min_hours_between_attempts=24.0
    )
    runner._audit_log = MagicMock()
    runner._audit_log.read_for_param.return_value = []
    runner._registry = MagicMock()
    runner._proposer = MagicMock()
    runner._proposer.propose.return_value = MagicMock(champion_value=1.0, challenger_value=1.1)
    runner._gate = MagicMock()
    runner._shadow_mode = True
    runner._store = MagicMock()

    with patch.object(runner, "_cooldown_active", return_value=(False, "")):
        with pytest.raises(Exception):  # noqa: B017 - evaluate_fn is a stub
            runner.attempt("p", MagicMock(side_effect=RuntimeError("stop here")), "m", 4_242)

    proposed = [
        c for c in runner._audit_log.record.call_args_list if c.args[1] == TuningEventType.PROPOSED
    ]
    assert proposed
    assert proposed[0].args[2]["closed_trade_count"] == 4_242
