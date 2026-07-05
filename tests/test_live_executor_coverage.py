"""
Coverage tests for LiveExecutor critical paths (Debt-009).

Uses object.__new__ to bypass TRADING_MODE=live gate (same pattern as
tests/test_order_fsm_registry.py — avoids lru_cache env var race).

Targets: initialize, submit_signal, _submit_signal_auto,
_submit_signal_with_approval, _place_and_record, close_position,
mark_to_market, reset_daily_equity, get_risk_snapshot, helper properties.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import ExecutionMode, TradingMode
from src.execution.live import LiveExecutor, LivePosition
from src.risk.kelly import KellyResult


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _kelly(
    notional: float = 5000.0,
    qty: float = 0.1,
    price: float = 50000.0,
) -> KellyResult:
    return KellyResult(
        kelly_fraction=0.02,
        adjusted_fraction=0.01,
        capital_usd=100_000.0,
        entry_price=price,
        quantity=qty,
        notional_usd=notional,
        is_capped=False,
    )


def _make_executor(
    starting_capital: float = 100_000.0,
    cash: float | None = None,
) -> LiveExecutor:
    """Construct LiveExecutor without __init__ (no TRADING_MODE=live needed)."""
    import asyncio
    from collections import OrderedDict

    from src.diagnostics.trade_auditor import DrawdownTracker
    from src.execution.order_manager import OrderManager
    import structlog

    executor = object.__new__(LiveExecutor)
    executor._starting_capital = starting_capital
    executor._cash = cash if cash is not None else starting_capital
    executor._peak_equity = starting_capital
    executor._positions: dict = {}
    executor._approval_queue: dict = {}
    executor._lock = asyncio.Lock()
    executor._trade_semaphore = asyncio.Semaphore(1)
    executor._drawdown_tracker = DrawdownTracker(starting_capital)
    executor._order_manager = OrderManager()
    executor._order_fsm_registry = OrderedDict()
    executor._initialized = True
    executor._storage = AsyncMock()
    executor._fetcher = MagicMock()
    executor._cfg = MagicMock()
    executor._risk_cfg = MagicMock(
        notional_limit_usd=10_000.0,
        approval_timeout_s=30.0,
    )
    executor._log = structlog.get_logger().bind(component="live_executor_test")
    return executor


def _filled_order(
    order_id: str = "ord-1",
    price: float = 50000.0,
    qty: float = 0.1,
    fee_usd: float = 2.5,
) -> dict:
    return {
        "id": order_id,
        "status": "closed",
        "filled": qty,
        "amount": qty,
        "average": price,
        "fees": [{"currency": "USDT", "cost": fee_usd}],
    }


# ─────────────────────────────────────────────────────────────
# initialize()
# ─────────────────────────────────────────────────────────────

class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_fresh_no_prior_equity(self):
        ex = _make_executor()
        ex._initialized = False
        ex._storage.latest_equity = AsyncMock(return_value=None)
        await ex.initialize()
        assert ex._initialized is True

    @pytest.mark.asyncio
    async def test_initialize_restores_from_storage(self):
        ex = _make_executor(starting_capital=100_000.0)
        ex._initialized = False
        record = MagicMock(
            cash_usd=80_000.0,
            equity_usd=85_000.0,
            peak_equity_usd=100_000.0,
        )
        ex._storage.latest_equity = AsyncMock(return_value=record)
        await ex.initialize()
        assert ex._cash == 80_000.0
        assert ex._initialized is True


# ─────────────────────────────────────────────────────────────
# Properties
# ─────────────────────────────────────────────────────────────

class TestProperties:
    def test_cash_usd(self):
        ex = _make_executor(cash=75_000.0)
        assert ex.cash_usd == 75_000.0

    def test_starting_capital(self):
        ex = _make_executor(starting_capital=50_000.0)
        assert ex.starting_capital == 50_000.0

    def test_peak_equity(self):
        ex = _make_executor()
        ex._peak_equity = 110_000.0
        assert ex.peak_equity == 110_000.0

    def test_position_count_empty(self):
        ex = _make_executor()
        assert ex.position_count == 0

    def test_position_count_with_positions(self):
        ex = _make_executor()
        ex._positions["BTC/USDT"] = MagicMock()
        assert ex.position_count == 1

    def test_equity_usd_no_positions(self):
        ex = _make_executor(cash=80_000.0)
        assert ex.equity_usd == 80_000.0

    def test_open_positions_snapshot_empty(self):
        ex = _make_executor()
        snap = ex._open_positions_snapshot()
        assert snap == []

    @pytest.mark.asyncio
    async def test_open_positions_safe(self):
        ex = _make_executor()
        result = await ex.open_positions_safe()
        assert result == []

    @pytest.mark.asyncio
    async def test_pending_approvals_safe_empty(self):
        ex = _make_executor()
        result = await ex.pending_approvals_safe()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_consecutive_losses(self):
        ex = _make_executor()
        ex._storage.count_consecutive_losses = AsyncMock(return_value=2)
        result = await ex.get_consecutive_losses("BTC/USDT")
        assert result == 2

    @pytest.mark.asyncio
    async def test_get_daily_pnl(self):
        ex = _make_executor()
        ex._storage.daily_pnl = AsyncMock(return_value=-150.0)
        result = await ex.get_daily_pnl("BTC/USDT")
        assert result == -150.0
