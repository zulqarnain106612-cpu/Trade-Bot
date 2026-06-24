"""Test coverage for src/execution/paper.py — paper trading executor."""

import asyncio
import os
import tempfile

import pytest

from src.config import ExecutionMode, runtime_config
from src.data.storage import StorageBackend
from src.execution.paper import ApprovalRequest, PaperExecutor, PaperPosition
from src.risk.kelly import KellyResult


@pytest.fixture
async def storage():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    sb = StorageBackend(db_path=path)
    await sb.initialize()
    yield sb
    await sb.close()
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(path + ext)
        except FileNotFoundError:
            pass


@pytest.fixture
async def executor(storage):
    ex = PaperExecutor(storage, starting_capital=10000.0)
    await ex.initialize()
    yield ex
    # Reset the module-level runtime_config singleton so tests don't leak state.
    await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)


def make_kelly(notional=500.0, price=100.0, fraction=0.1):
    qty = notional / price
    return KellyResult(
        kelly_fraction=fraction * 2,
        adjusted_fraction=fraction,
        capital_usd=10000.0,
        entry_price=price,
        quantity=qty,
        notional_usd=notional,
        is_capped=False,
    )


class TestPaperPosition:
    """PaperPosition.mark() unrealized PnL calculation."""

    def test_mark_long_profit(self):
        pos = PaperPosition(
            trade_id="t1", symbol="BTC/USDT", timeframe="15m", direction=1,
            entry_price=100.0, quantity=2.0, notional_usd=200.0, entry_ts=1000,
            kelly_fraction=0.1, regime_at_entry=1, meta_label_prob=0.6,
            raw_signal=0.5, approved_by="auto", execution_mode="automatic", fee_usd=0.2,
        )
        pnl = pos.mark(110.0)
        assert pnl == pytest.approx(20.0)
        assert pos.current_price == 110.0

    def test_mark_long_loss(self):
        pos = PaperPosition(
            trade_id="t1", symbol="BTC/USDT", timeframe="15m", direction=1,
            entry_price=100.0, quantity=2.0, notional_usd=200.0, entry_ts=1000,
            kelly_fraction=0.1, regime_at_entry=1, meta_label_prob=0.6,
            raw_signal=0.5, approved_by="auto", execution_mode="automatic", fee_usd=0.2,
        )
        pnl = pos.mark(90.0)
        assert pnl == pytest.approx(-20.0)

    def test_mark_short_profit(self):
        pos = PaperPosition(
            trade_id="t1", symbol="BTC/USDT", timeframe="15m", direction=0,
            entry_price=100.0, quantity=2.0, notional_usd=200.0, entry_ts=1000,
            kelly_fraction=0.1, regime_at_entry=1, meta_label_prob=0.6,
            raw_signal=0.5, approved_by="auto", execution_mode="automatic", fee_usd=0.2,
        )
        pnl = pos.mark(90.0)
        assert pnl == pytest.approx(20.0)

    def test_mark_short_loss(self):
        pos = PaperPosition(
            trade_id="t1", symbol="BTC/USDT", timeframe="15m", direction=0,
            entry_price=100.0, quantity=2.0, notional_usd=200.0, entry_ts=1000,
            kelly_fraction=0.1, regime_at_entry=1, meta_label_prob=0.6,
            raw_signal=0.5, approved_by="auto", execution_mode="automatic", fee_usd=0.2,
        )
        pnl = pos.mark(110.0)
        assert pnl == pytest.approx(-20.0)


class TestApprovalRequestToDict:
    """ApprovalRequest.to_dict() serialization."""

    def test_to_dict_long(self):
        req = ApprovalRequest(
            request_id="r1", symbol="BTC/USDT", timeframe="15m", direction=1,
            notional_usd=500.0, entry_price=100.0, quantity=5.0,
            kelly_fraction=0.1, regime_state=1, meta_label_prob=0.6,
            raw_signal=0.5, created_at=0.0,
        )
        d = req.to_dict()
        assert d["direction"] == "long"
        assert d["notional_usd"] == 500.0

    def test_to_dict_short(self):
        req = ApprovalRequest(
            request_id="r1", symbol="BTC/USDT", timeframe="15m", direction=0,
            notional_usd=500.0, entry_price=100.0, quantity=5.0,
            kelly_fraction=0.1, regime_state=1, meta_label_prob=0.6,
            raw_signal=0.5, created_at=0.0,
        )
        d = req.to_dict()
        assert d["direction"] == "short"


class TestLifecycle:
    """initialize, shutdown, fresh vs restored state."""

    @pytest.mark.asyncio
    async def test_initialize_fresh_no_prior_state(self, storage):
        ex = PaperExecutor(storage, starting_capital=5000.0)
        await ex.initialize()
        assert ex.cash_usd == 5000.0
        assert ex.equity_usd == 5000.0
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)

    @pytest.mark.asyncio
    async def test_initialize_restores_from_storage(self, storage):
        ex1 = PaperExecutor(storage, starting_capital=5000.0)
        await ex1.initialize()
        kelly = make_kelly(notional=500.0, price=100.0)
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        await ex1.submit_signal("BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0)

        ex2 = PaperExecutor(storage, starting_capital=5000.0)
        await ex2.initialize()
        assert ex2.cash_usd == pytest.approx(ex1.cash_usd)

    @pytest.mark.asyncio
    async def test_shutdown_persists_snapshot(self, executor, storage):
        await executor.shutdown()
        latest = await storage.latest_equity("paper")
        assert latest is not None

    @pytest.mark.asyncio
    async def test_require_initialized_raises_before_init(self, storage):
        ex = PaperExecutor(storage, starting_capital=5000.0)
        with pytest.raises(RuntimeError, match="not initialized"):
            await ex.submit_signal("BTC/USDT", "15m", 1, make_kelly(), 1, 0.6, 0.5, 100.0)

    @pytest.mark.asyncio
    async def test_starting_capital_default_from_settings(self, storage):
        ex = PaperExecutor(storage)  # no explicit starting_capital
        await ex.initialize()
        assert ex.starting_capital > 0


class TestSubmitSignalAutomatic:
    """AUTOMATIC mode — fires immediately."""

    @pytest.mark.asyncio
    async def test_automatic_opens_position(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        kelly = make_kelly(notional=500.0, price=100.0)
        trade_id, outcome = await executor.submit_signal(
            "BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0
        )
        assert outcome == "opened"
        assert trade_id is not None
        assert executor.position_count() == 1

    @pytest.mark.asyncio
    async def test_automatic_rejects_insufficient_cash(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        kelly = make_kelly(notional=50000.0, price=100.0)  # more than 10000 cash
        trade_id, outcome = await executor.submit_signal(
            "BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0
        )
        assert outcome == "rejected"
        assert trade_id is None

    @pytest.mark.asyncio
    async def test_automatic_deducts_cash(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        starting_cash = executor.cash_usd
        kelly = make_kelly(notional=500.0, price=100.0)
        await executor.submit_signal("BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0)
        assert executor.cash_usd < starting_cash


class TestSubmitSignalRestricted:
    """RESTRICTED mode — auto below limit, approval above."""

    @pytest.mark.asyncio
    async def test_restricted_below_limit_auto_opens(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.RESTRICTED)
        kelly = make_kelly(notional=50.0, price=100.0)  # below default 100.0 limit
        trade_id, outcome = await executor.submit_signal(
            "BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0
        )
        assert outcome == "opened"
        assert trade_id is not None

    @pytest.mark.asyncio
    async def test_restricted_above_limit_queues_then_approves(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.RESTRICTED)
        kelly = make_kelly(notional=500.0, price=100.0)  # above default 100.0 limit

        async def approve_soon():
            await asyncio.sleep(0.05)
            pending = executor.pending_approvals()
            assert len(pending) == 1
            req_id = pending[0]["request_id"]
            await executor.resolve_approval(req_id, approved=True, operator="alice")

        approve_task = asyncio.create_task(approve_soon())
        trade_id, outcome = await executor.submit_signal(
            "BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0
        )
        await approve_task
        assert outcome == "opened"
        assert trade_id is not None

    @pytest.mark.asyncio
    async def test_restricted_above_limit_rejected_by_operator(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.RESTRICTED)
        kelly = make_kelly(notional=500.0, price=100.0)

        async def reject_soon():
            await asyncio.sleep(0.05)
            pending = executor.pending_approvals()
            req_id = pending[0]["request_id"]
            await executor.resolve_approval(req_id, approved=False, operator="bob")

        reject_task = asyncio.create_task(reject_soon())
        trade_id, outcome = await executor.submit_signal(
            "BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0
        )
        await reject_task
        assert outcome == "skipped"
        assert trade_id is None


class TestSubmitSignalManual:
    """MANUAL mode — every trade queued."""

    @pytest.mark.asyncio
    async def test_manual_queues_and_approves(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.MANUAL)
        kelly = make_kelly(notional=50.0, price=100.0)

        async def approve_soon():
            await asyncio.sleep(0.05)
            pending = executor.pending_approvals()
            req_id = pending[0]["request_id"]
            await executor.resolve_approval(req_id, approved=True, operator="carol")

        approve_task = asyncio.create_task(approve_soon())
        trade_id, outcome = await executor.submit_signal(
            "BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0
        )
        await approve_task
        assert outcome == "opened"

    @pytest.mark.asyncio
    async def test_manual_rejected_by_operator(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.MANUAL)
        kelly = make_kelly(notional=50.0, price=100.0)

        async def reject_soon():
            await asyncio.sleep(0.05)
            pending = executor.pending_approvals()
            req_id = pending[0]["request_id"]
            await executor.resolve_approval(req_id, approved=False, operator="dave")

        reject_task = asyncio.create_task(reject_soon())
        trade_id, outcome = await executor.submit_signal(
            "BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0
        )
        await reject_task
        assert outcome == "rejected"
        assert trade_id is None


class TestClosePosition:
    """close_position — PnL computation and persistence."""

    @pytest.mark.asyncio
    async def test_close_long_profit(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        kelly = make_kelly(notional=500.0, price=100.0)
        trade_id, _ = await executor.submit_signal(
            "BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0
        )
        net_pnl = await executor.close_position(trade_id, exit_price=110.0, exit_reason="profit_target")
        assert net_pnl > 0
        assert executor.position_count() == 0

    @pytest.mark.asyncio
    async def test_close_long_loss(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        kelly = make_kelly(notional=500.0, price=100.0)
        trade_id, _ = await executor.submit_signal(
            "BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0
        )
        net_pnl = await executor.close_position(trade_id, exit_price=90.0, exit_reason="stop_loss")
        assert net_pnl < 0

    @pytest.mark.asyncio
    async def test_close_short_profit(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        kelly = make_kelly(notional=500.0, price=100.0)
        trade_id, _ = await executor.submit_signal(
            "BTC/USDT", "15m", 0, kelly, 1, 0.6, 0.5, 100.0
        )
        net_pnl = await executor.close_position(trade_id, exit_price=90.0, exit_reason="profit_target")
        assert net_pnl > 0

    @pytest.mark.asyncio
    async def test_close_returns_cash_to_pool(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        cash_before_open = executor.cash_usd
        kelly = make_kelly(notional=500.0, price=100.0)
        trade_id, _ = await executor.submit_signal(
            "BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0
        )
        cash_after_open = executor.cash_usd
        assert cash_after_open < cash_before_open
        await executor.close_position(trade_id, exit_price=100.0, exit_reason="time_exit")
        cash_after_close = executor.cash_usd
        assert cash_after_close > cash_after_open

    @pytest.mark.asyncio
    async def test_close_unknown_trade_id_raises(self, executor):
        with pytest.raises(KeyError, match="No open paper position"):
            await executor.close_position("nonexistent", exit_price=100.0, exit_reason="manual")

    @pytest.mark.asyncio
    async def test_close_persists_to_storage(self, executor, storage):
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        kelly = make_kelly(notional=500.0, price=100.0)
        trade_id, _ = await executor.submit_signal(
            "BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0
        )
        await executor.close_position(trade_id, exit_price=110.0, exit_reason="profit_target")
        trades = await storage.fetch_trades(symbol="BTC/USDT")
        assert trades[0].exit_price == 110.0
        assert trades[0].exit_reason == "profit_target"


class TestMarkToMarket:
    """mark_to_market — unrealized PnL updates."""

    @pytest.mark.asyncio
    async def test_mark_to_market_updates_unrealized(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        kelly = make_kelly(notional=500.0, price=100.0)
        await executor.submit_signal("BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0)
        total_unrealized = await executor.mark_to_market({"BTC/USDT": 110.0})
        assert total_unrealized > 0

    @pytest.mark.asyncio
    async def test_mark_to_market_no_matching_price_skips(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        kelly = make_kelly(notional=500.0, price=100.0)
        await executor.submit_signal("BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0)
        total_unrealized = await executor.mark_to_market({"ETH/USDT": 2000.0})
        assert total_unrealized == 0.0

    @pytest.mark.asyncio
    async def test_mark_to_market_zero_price_skips(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        kelly = make_kelly(notional=500.0, price=100.0)
        await executor.submit_signal("BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0)
        total_unrealized = await executor.mark_to_market({"BTC/USDT": 0.0})
        assert total_unrealized == 0.0

    @pytest.mark.asyncio
    async def test_mark_to_market_no_positions(self, executor):
        total_unrealized = await executor.mark_to_market({"BTC/USDT": 100.0})
        assert total_unrealized == 0.0

    @pytest.mark.asyncio
    async def test_mark_to_market_updates_peak_equity(self, executor):
        """Large favorable move pushes equity (and therefore peak) above the
        post-entry level (which sits slightly below starting capital due to
        the entry fee)."""
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        kelly = make_kelly(notional=500.0, price=100.0)
        await executor.submit_signal("BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0)
        equity_after_entry = executor.equity_usd
        await executor.mark_to_market({"BTC/USDT": 1000.0})  # 10x move
        assert executor.peak_equity > equity_after_entry
        assert executor.peak_equity > executor.starting_capital


class TestApprovalQueueManagement:
    """resolve_approval, pending_approvals, pruning."""

    @pytest.mark.asyncio
    async def test_resolve_unknown_request_returns_false(self, executor):
        result = await executor.resolve_approval("ghost", approved=True, operator="alice")
        assert result is False

    @pytest.mark.asyncio
    async def test_resolve_already_resolved_returns_false(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.MANUAL)
        kelly = make_kelly(notional=50.0, price=100.0)

        async def double_resolve():
            await asyncio.sleep(0.05)
            pending = executor.pending_approvals()
            req_id = pending[0]["request_id"]
            first = await executor.resolve_approval(req_id, approved=True, operator="alice")
            second = await executor.resolve_approval(req_id, approved=True, operator="bob")
            assert first is True
            assert second is False

        task = asyncio.create_task(double_resolve())
        await executor.submit_signal("BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0)
        await task

    @pytest.mark.asyncio
    async def test_pending_approvals_empty_initially(self, executor):
        assert executor.pending_approvals() == []

    @pytest.mark.asyncio
    async def test_pending_approvals_safe(self, executor):
        result = await executor.pending_approvals_safe()
        assert result == []


class TestApprovalTimeout:
    """RESTRICTED mode approval timeout auto-skip."""

    @pytest.mark.asyncio
    async def test_restricted_timeout_auto_skips(self, storage, monkeypatch):
        ex = PaperExecutor(storage, starting_capital=10000.0)
        await ex.initialize()
        await runtime_config.set_execution_mode(ExecutionMode.RESTRICTED)
        ex._risk_cfg.approval_timeout_s = 0.05  # speed up test
        kelly = make_kelly(notional=500.0, price=100.0)
        trade_id, outcome = await ex.submit_signal(
            "BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0
        )
        assert outcome == "skipped"
        assert trade_id is None
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)


class TestStateQueriesAndProperties:
    """open_positions, open_positions_safe, properties, risk snapshot."""

    @pytest.mark.asyncio
    async def test_open_positions_empty_initially(self, executor):
        assert executor.open_positions() == []

    @pytest.mark.asyncio
    async def test_open_positions_after_open(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        kelly = make_kelly(notional=500.0, price=100.0)
        await executor.submit_signal("BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0)
        positions = executor.open_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTC/USDT"
        assert positions[0]["direction"] == "long"

    @pytest.mark.asyncio
    async def test_open_positions_safe_matches_open_positions(self, executor):
        await runtime_config.set_execution_mode(ExecutionMode.AUTOMATIC)
        kelly = make_kelly(notional=500.0, price=100.0)
        await executor.submit_signal("BTC/USDT", "15m", 1, kelly, 1, 0.6, 0.5, 100.0)
        safe = await executor.open_positions_safe()
        unsafe = executor.open_positions()
        assert safe[0]["trade_id"] == unsafe[0]["trade_id"]

    @pytest.mark.asyncio
    async def test_cash_usd_property(self, executor):
        assert executor.cash_usd == 10000.0

    @pytest.mark.asyncio
    async def test_equity_usd_property(self, executor):
        assert executor.equity_usd == 10000.0

    @pytest.mark.asyncio
    async def test_starting_capital_property(self, executor):
        assert executor.starting_capital == 10000.0

    @pytest.mark.asyncio
    async def test_peak_equity_property(self, executor):
        assert executor.peak_equity == 10000.0

    @pytest.mark.asyncio
    async def test_drawdown_tracker_property(self, executor):
        assert executor.drawdown_tracker is not None

    @pytest.mark.asyncio
    async def test_starting_equity_usd_property(self, executor):
        assert executor.starting_equity_usd == 10000.0

    @pytest.mark.asyncio
    async def test_position_count_zero_initially(self, executor):
        assert executor.position_count() == 0

    @pytest.mark.asyncio
    async def test_reset_daily_equity(self, executor):
        equity = await executor.reset_daily_equity()
        assert equity == 10000.0

    @pytest.mark.asyncio
    async def test_get_risk_snapshot(self, executor):
        equity, start_eq, daily_pnl = await executor.get_risk_snapshot()
        assert equity == 10000.0
        assert start_eq == 10000.0
        assert daily_pnl == 0.0

    @pytest.mark.asyncio
    async def test_get_consecutive_losses_delegates_to_storage(self, executor):
        losses = await executor.get_consecutive_losses("BTC/USDT")
        assert losses == 0

    @pytest.mark.asyncio
    async def test_get_daily_pnl_delegates_to_storage(self, executor):
        pnl = await executor.get_daily_pnl("BTC/USDT")
        assert pnl == 0.0
