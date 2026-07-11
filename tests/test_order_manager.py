"""Tests for src/execution/order_manager.py — OrderManager fill confirmation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.order_fsm import OrderStatus
from src.execution.order_manager import OrderManager


def _make_exchange(create_result: dict, fetch_result: dict) -> MagicMock:
    exchange = MagicMock()
    exchange.create_market_order = AsyncMock(return_value=create_result)
    exchange.fetch_order = AsyncMock(return_value=fetch_result)
    return exchange


class TestConfirmOrderFill:
    @pytest.mark.asyncio
    async def test_filled_with_average_price_succeeds(self):
        manager = OrderManager()
        exchange = _make_exchange(
            create_result={"id": "ord-1"},
            fetch_result={"status": "filled", "filled": 1.0, "average": 100.0},
        )
        fsm, confirmed = await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)
        assert fsm.state.status == OrderStatus.FILLED
        assert confirmed["average"] == 100.0

    @pytest.mark.asyncio
    async def test_filled_with_price_fallback_succeeds(self):
        manager = OrderManager()
        exchange = _make_exchange(
            create_result={"id": "ord-2"},
            fetch_result={"status": "closed", "filled": 1.0, "price": 99.5},
        )
        fsm, _confirmed = await manager.place_order_with_fsm(exchange, "BTC/USDT", "sell", 1.0)
        assert fsm.state.status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_filled_with_no_fill_price_raises_and_fails_fsm(self):
        """UI-009: a 'filled' exchange response with neither `average` nor
        `price` must never be silently recorded as avg_price=0.0 -- that
        would corrupt PnL/notional accounting. It must raise and leave the
        FSM in FAILED, not FILLED."""
        manager = OrderManager()
        exchange = _make_exchange(
            create_result={"id": "ord-3"},
            fetch_result={"status": "filled", "filled": 1.0},  # no average, no price
        )
        with pytest.raises(ValueError, match="cannot safely record"):
            await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)

    @pytest.mark.asyncio
    async def test_filled_with_zero_fill_price_field_raises(self):
        """A real fill is never legitimately priced at exactly 0.0 -- an
        explicit average=0.0/price=0.0 must be rejected the same way a
        missing field is, not silently accepted as a free fill."""
        manager = OrderManager()
        exchange = _make_exchange(
            create_result={"id": "ord-4"},
            fetch_result={"status": "filled", "filled": 1.0, "average": 0.0, "price": 0.0},
        )
        with pytest.raises(ValueError, match="cannot safely record"):
            await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)
