"""Additional coverage for src/execution/live.py uncovered paths."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.live import ApprovalRequest, LiveExecutor, LivePosition
from src.execution.order_manager import OrderManager
from src.risk.gates import DrawdownTracker


def _make_executor(starting_capital: float = 100_000.0, cash: float | None = None) -> LiveExecutor:
    import structlog

    ex = object.__new__(LiveExecutor)
    ex._starting_capital = starting_capital
    ex._cash = cash if cash is not None else starting_capital
    ex._peak_equity = starting_capital
    ex._positions = {}
    ex._approval_queue = {}
    ex._lock = asyncio.Lock()
    ex._trade_semaphore = asyncio.Semaphore(1)
    ex._drawdown_tracker = DrawdownTracker(starting_capital)
    ex._order_manager = OrderManager()
    ex._order_fsm_registry = OrderedDict()
    ex._initialized = True
    ex._storage = AsyncMock()
    ex._fetcher = MagicMock()
    # v8: initialize() reconciles against exchange truth; an unavailable
    # snapshot blocks new entries, so give it an explicit empty book.
    ex._fetcher.fetch_exchange_positions = AsyncMock(return_value=[])
    ex._recovery_discrepancies = []
    ex._cfg = MagicMock()
    ex._risk_cfg = MagicMock(notional_limit_usd=10_000.0, approval_timeout_s=30.0)
    ex._log = structlog.get_logger().bind(component="test")
    return ex


def _make_position(trade_id="t1", direction=1, entry_price=50_000.0, quantity=0.1):
    return LivePosition(
        trade_id=trade_id,
        exchange_order_id="ord-1",
        symbol="BTC/USDT",
        timeframe="15m",
        direction=direction,
        entry_price=entry_price,
        quantity=quantity,
        notional_usd=entry_price * quantity,
        entry_ts=int(time.time() * 1000),
        kelly_fraction=0.02,
        regime_at_entry=1,
        meta_label_prob=0.6,
        raw_signal=0.7,
        approved_by="auto",
        execution_mode="automatic",
        fee_usd=1.0,
    )


# ---------------------------------------------------------------------------
# LivePosition.mark() — short path
# ---------------------------------------------------------------------------


def test_live_position_mark_long():
    pos = _make_position(direction=1, entry_price=50_000.0, quantity=0.1)
    pnl = pos.mark(51_000.0)
    assert pnl == pytest.approx(100.0)  # (51000 - 50000) * 0.1


def test_live_position_mark_short():
    pos = _make_position(direction=-1, entry_price=50_000.0, quantity=0.1)
    pnl = pos.mark(49_000.0)
    assert pnl == pytest.approx(100.0)  # (50000 - 49000) * 0.1


def test_live_position_mark_short_loss():
    pos = _make_position(direction=-1, entry_price=50_000.0, quantity=0.1)
    pnl = pos.mark(51_000.0)
    assert pnl == pytest.approx(-100.0)  # (50000 - 51000) * 0.1


# ---------------------------------------------------------------------------
# ApprovalRequest.to_dict()
# ---------------------------------------------------------------------------


def test_approval_request_to_dict():
    req = ApprovalRequest(
        request_id="req-1",
        symbol="BTC/USDT",
        timeframe="15m",
        direction=1,
        notional_usd=5000.0,
        entry_price=50_000.0,
        quantity=0.1,
        kelly_fraction=0.02,
        regime_state=1,
        meta_label_prob=0.65,
        raw_signal=0.72,
        created_at=time.monotonic(),
    )
    d = req.to_dict()
    assert d["request_id"] == "req-1"
    assert d["direction"] == "long"
    assert "notional_usd" in d
    assert "regime_state" in d


def test_approval_request_short_direction():
    req = ApprovalRequest(
        request_id="req-2",
        symbol="BTC/USDT",
        timeframe="15m",
        direction=-1,
        notional_usd=5000.0,
        entry_price=50_000.0,
        quantity=0.1,
        kelly_fraction=0.02,
        regime_state=0,
        meta_label_prob=0.6,
        raw_signal=0.3,
        created_at=time.monotonic(),
    )
    d = req.to_dict()
    assert d["direction"] == "short"


# ---------------------------------------------------------------------------
# LiveExecutor.shutdown() with positions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_with_no_positions():
    ex = _make_executor()
    ex._storage.insert_equity = AsyncMock()
    ex._storage.latest_equity = AsyncMock(return_value=None)
    await ex.shutdown()


@pytest.mark.asyncio
async def test_shutdown_with_open_positions():
    ex = _make_executor(cash=90_000.0)
    ex._positions["t1"] = _make_position("t1")
    ex._storage.insert_equity = AsyncMock()
    ex._storage.latest_equity = AsyncMock(return_value=None)
    await ex.shutdown()


# ---------------------------------------------------------------------------
# resolve_approval()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_approval_success():
    ex = _make_executor()
    req = ApprovalRequest(
        request_id="req-1",
        symbol="BTC/USDT",
        timeframe="15m",
        direction=1,
        notional_usd=5000.0,
        entry_price=50_000.0,
        quantity=0.1,
        kelly_fraction=0.02,
        regime_state=1,
        meta_label_prob=0.6,
        raw_signal=0.7,
        created_at=time.monotonic(),
    )
    ex._approval_queue["req-1"] = req

    result = await ex.resolve_approval("req-1", approved=True, operator="alice")
    assert result is True
    assert req.resolved is True
    assert req.approved is True
    assert req.operator == "alice"
    assert req._event.is_set()


@pytest.mark.asyncio
async def test_resolve_approval_not_found_returns_false():
    ex = _make_executor()
    result = await ex.resolve_approval("nonexistent", approved=True, operator="bob")
    assert result is False


@pytest.mark.asyncio
async def test_resolve_approval_already_resolved_returns_false():
    ex = _make_executor()
    req = ApprovalRequest(
        request_id="req-1",
        symbol="BTC/USDT",
        timeframe="15m",
        direction=1,
        notional_usd=5000.0,
        entry_price=50_000.0,
        quantity=0.1,
        kelly_fraction=0.02,
        regime_state=1,
        meta_label_prob=0.6,
        raw_signal=0.7,
        created_at=time.monotonic(),
        resolved=True,
    )
    ex._approval_queue["req-1"] = req

    result = await ex.resolve_approval("req-1", approved=True, operator="bob")
    assert result is False


# ---------------------------------------------------------------------------
# _pending_approvals_unsafe()
# ---------------------------------------------------------------------------


def test_pending_approvals_unsafe_empty():
    ex = _make_executor()
    result = ex._pending_approvals_unsafe()
    assert result == []


def test_pending_approvals_unsafe_unresolved():
    ex = _make_executor()
    req = ApprovalRequest(
        request_id="req-1",
        symbol="BTC/USDT",
        timeframe="15m",
        direction=1,
        notional_usd=5000.0,
        entry_price=50_000.0,
        quantity=0.1,
        kelly_fraction=0.02,
        regime_state=1,
        meta_label_prob=0.6,
        raw_signal=0.7,
        created_at=time.monotonic(),
    )
    ex._approval_queue["req-1"] = req
    result = ex._pending_approvals_unsafe()
    assert len(result) == 1
    assert result[0]["request_id"] == "req-1"


def test_pending_approvals_unsafe_prunes_stale_resolved():
    ex = _make_executor()
    stale_req = ApprovalRequest(
        request_id="stale",
        symbol="BTC/USDT",
        timeframe="15m",
        direction=1,
        notional_usd=5000.0,
        entry_price=50_000.0,
        quantity=0.1,
        kelly_fraction=0.02,
        regime_state=1,
        meta_label_prob=0.6,
        raw_signal=0.7,
        created_at=time.monotonic() - 7200.0,  # 2 hours old
        resolved=True,
    )
    ex._approval_queue["stale"] = stale_req
    result = ex._pending_approvals_unsafe()
    assert len(result) == 0
    assert "stale" not in ex._approval_queue


def test_pending_approvals_sync():
    ex = _make_executor()
    result = ex.pending_approvals()
    assert result == []


# ---------------------------------------------------------------------------
# open_positions() and properties
# ---------------------------------------------------------------------------


def test_open_positions_unsync():
    ex = _make_executor()
    assert ex.open_positions() == []


def test_open_positions_with_positions():
    ex = _make_executor(cash=90_000.0)
    pos = _make_position()
    pos.mark(51_000.0)
    ex._positions["t1"] = pos
    result = ex.open_positions()
    assert len(result) == 1
    assert result[0]["direction"] == "long"


def test_drawdown_tracker_property():
    ex = _make_executor()
    assert ex.drawdown_tracker is ex._drawdown_tracker


def test_starting_equity_usd():
    ex = _make_executor(starting_capital=50_000.0)
    assert ex.starting_equity_usd == pytest.approx(50_000.0)


# ---------------------------------------------------------------------------
# reset_daily_equity() and get_risk_snapshot()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_daily_equity():
    ex = _make_executor(cash=95_000.0)
    equity = await ex.reset_daily_equity()
    assert equity == pytest.approx(95_000.0)


@pytest.mark.asyncio
async def test_get_risk_snapshot():
    ex = _make_executor(cash=95_000.0)
    equity, start, pnl = await ex.get_risk_snapshot()
    assert equity == pytest.approx(95_000.0)
    assert isinstance(start, float)
    assert isinstance(pnl, float)


# ---------------------------------------------------------------------------
# _await_approval() — timeout path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_approval_missing_request_returns_false():
    ex = _make_executor()
    result, operator = await ex._await_approval("nonexistent", timeout_s=0.01)
    assert result is False
    assert operator == ""


@pytest.mark.asyncio
async def test_await_approval_timeout_returns_false():
    ex = _make_executor()
    req = ApprovalRequest(
        request_id="req-1",
        symbol="BTC/USDT",
        timeframe="15m",
        direction=1,
        notional_usd=5000.0,
        entry_price=50_000.0,
        quantity=0.1,
        kelly_fraction=0.02,
        regime_state=1,
        meta_label_prob=0.6,
        raw_signal=0.7,
        created_at=time.monotonic(),
    )
    ex._approval_queue["req-1"] = req

    result, operator = await ex._await_approval("req-1", timeout_s=0.01)
    assert result is False
    assert operator == "auto_timeout"


@pytest.mark.asyncio
async def test_await_approval_success():
    ex = _make_executor()
    req = ApprovalRequest(
        request_id="req-2",
        symbol="BTC/USDT",
        timeframe="15m",
        direction=1,
        notional_usd=5000.0,
        entry_price=50_000.0,
        quantity=0.1,
        kelly_fraction=0.02,
        regime_state=1,
        meta_label_prob=0.6,
        raw_signal=0.7,
        created_at=time.monotonic(),
    )
    ex._approval_queue["req-2"] = req

    async def _resolve():
        await asyncio.sleep(0.01)
        req.approved = True
        req.operator = "alice"
        req.resolved = True
        req._event.set()

    task = asyncio.create_task(_resolve())
    result, operator = await ex._await_approval("req-2", timeout_s=1.0)
    await task
    assert result is True
    assert operator == "alice"
