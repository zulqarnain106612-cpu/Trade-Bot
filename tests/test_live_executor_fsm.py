"""
Integration tests for LiveExecutor with OrderFSM.

Tests that _place_market_order now uses OrderManager with FSM state tracking.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.execution.order_fsm import OrderFSM, OrderFSMState, OrderStatus
from src.execution.order_manager import OrderManager


class TestOrderManagerMock:
    """Test OrderManager in isolation with mocked exchange."""

    @pytest.mark.asyncio
    async def test_place_order_success_immediate_fill(self):
        """Order placed and immediately filled."""
        manager = OrderManager()

        # Mock exchange that returns filled order immediately
        mock_exchange = AsyncMock()
        mock_exchange.create_market_order = AsyncMock(
            return_value={
                "id": "12345",
                "status": "closed",
                "filled": 1.5,
                "amount": 1.5,
                "average": 65000.0,
            }
        )
        mock_exchange.fetch_order = AsyncMock(
            return_value={
                "id": "12345",
                "status": "closed",
                "filled": 1.5,
                "amount": 1.5,
                "average": 65000.0,
            }
        )

        fsm, order = await manager.place_order_with_fsm(
            exchange=mock_exchange,
            symbol="BTC/USDT",
            side="buy",
            quantity=1.5,
        )

        assert fsm.state.status == OrderStatus.FILLED
        assert fsm.state.filled_qty == 1.5
        assert fsm.state.average_fill_price == 65000.0
        assert fsm.state.retry_count == 1  # One poll attempt
        assert order["status"] == "closed"

    @pytest.mark.asyncio
    async def test_place_order_pending_then_filled(self):
        """Order pending initially, then filled on subsequent poll."""
        manager = OrderManager()

        # Mock exchange: first fetch returns pending, second returns filled
        mock_exchange = AsyncMock()
        mock_exchange.create_market_order = AsyncMock(
            return_value={
                "id": "12346",
                "status": "open",
                "filled": 0.0,
            }
        )

        pending_order = {"id": "12346", "status": "open", "filled": 0.0}
        filled_order = {
            "id": "12346",
            "status": "closed",
            "filled": 1.5,
            "average": 65100.0,
        }

        # Simulate: first poll returns pending, second returns filled
        mock_exchange.fetch_order = AsyncMock(
            side_effect=[pending_order, filled_order]
        )

        fsm, order = await manager.place_order_with_fsm(
            exchange=mock_exchange,
            symbol="BTC/USDT",
            side="buy",
            quantity=1.5,
        )

        assert fsm.state.status == OrderStatus.FILLED
        assert fsm.state.filled_qty == 1.5
        assert fsm.state.retry_count == 2  # Two polls
        assert fsm.state.first_confirmed_at_ms is not None

    @pytest.mark.asyncio
    async def test_place_order_timeout_on_confirmation(self):
        """Order placed but confirmation times out."""
        manager = OrderManager()

        # Mock exchange that always returns pending
        mock_exchange = AsyncMock()
        mock_exchange.create_market_order = AsyncMock(
            return_value={"id": "12347", "status": "open", "filled": 0.0}
        )

        # Always return pending (never fills)
        mock_exchange.fetch_order = AsyncMock(
            return_value={"id": "12347", "status": "open", "filled": 0.0}
        )

        with pytest.raises(asyncio.TimeoutError):
            await manager.place_order_with_fsm(
                exchange=mock_exchange,
                symbol="BTC/USDT",
                side="buy",
                quantity=1.5,
            )

    @pytest.mark.asyncio
    async def test_place_order_network_error_recovery(self):
        """Network error during confirmation, recovers on retry."""
        manager = OrderManager()

        import ccxt

        mock_exchange = AsyncMock()
        mock_exchange.create_market_order = AsyncMock(
            return_value={"id": "12348", "status": "open", "filled": 0.0}
        )

        # Simulate: first poll fails with network error, second succeeds
        filled_order = {
            "id": "12348",
            "status": "closed",
            "filled": 1.5,
            "average": 65050.0,
        }

        mock_exchange.fetch_order = AsyncMock(
            side_effect=[
                ccxt.NetworkError("Connection timeout"),
                filled_order,
            ]
        )

        fsm, order = await manager.place_order_with_fsm(
            exchange=mock_exchange,
            symbol="BTC/USDT",
            side="buy",
            quantity=1.5,
        )

        # Should recover from network error
        assert fsm.state.status == OrderStatus.FILLED
        assert fsm.state.filled_qty == 1.5
        assert fsm.state.retry_count == 2  # Retried after network error


class TestFSMStateTransitions:
    """Test FSM state machine in context of order placement."""

    def test_order_fsm_state_progression(self):
        """Verify FSM state progression during order lifecycle."""
        # Start with PENDING
        state = OrderFSMState(
            order_id="99001",
            symbol="BTC/USDT",
            side="buy",
            quantity=2.0,
            status=OrderStatus.PENDING,
        )
        fsm = OrderFSM(state)

        # Transition: PENDING → FILLING (order confirmed by exchange)
        fsm.transition(OrderStatus.FILLING, {
            "exchange_response": {"id": "99001", "status": "open"}
        })
        assert fsm.state.status == OrderStatus.FILLING
        assert fsm.state.first_confirmed_at_ms is not None

        # Add partial fill
        fsm.add_partial_fill(0.8, 65000.0)
        assert fsm.state.filled_qty == 0.8
        assert fsm.state.average_fill_price == 65000.0

        # Add more partial fill
        fsm.add_partial_fill(1.2, 65100.0)
        assert fsm.state.filled_qty == 2.0
        # VWAP should be (0.8*65000 + 1.2*65100) / 2.0
        expected_vwap = (52000 + 78120) / 2.0
        assert abs(fsm.state.average_fill_price - expected_vwap) < 0.01

        # Transition: FILLING → FILLED (fully filled)
        fsm.transition(OrderStatus.FILLED, {
            "filled_qty": 2.0,
            "average_price": fsm.state.average_fill_price,
        })
        assert fsm.state.status == OrderStatus.FILLED
        assert fsm.state.is_terminal()

    def test_order_timeout_preserves_partial_fill(self):
        """Order timeout after partial fill — state preserved."""
        state = OrderFSMState(
            order_id="99002",
            symbol="ETH/USDT",
            side="sell",
            quantity=5.0,
            status=OrderStatus.FILLING,
            first_confirmed_at_ms=1234567890,
            filled_qty=2.5,
        )
        fsm = OrderFSM(state)

        # Add partial fill
        fsm.add_partial_fill(2.5, 3500.0)
        assert fsm.state.filled_qty == 5.0

        # Timeout while attempting to confirm
        fsm.transition(OrderStatus.TIMEOUT)

        # Partial fill should still be recorded
        assert fsm.state.status == OrderStatus.TIMEOUT
        assert fsm.state.filled_qty == 5.0  # Full fill preserved
        assert fsm.state.is_terminal()


class TestOrderReconciliation:
    """Test order reconciliation via FSM state snapshot."""

    def test_order_state_serialization(self):
        """Order FSM state can be serialized and deserialized."""
        # BUGFIX (found during audit, 2026-06-25): this test originally
        # constructed the state already in the terminal FILLED status, then
        # called add_partial_fill() on it -- but add_partial_fill() correctly
        # requires FILLING state (you can't add a fill to an order that's
        # already fully filled), so this raised OrderFSMError before
        # serialization was ever exercised. The test's actual intent is to
        # verify to_dict() serializes a populated filled_at_prices list
        # correctly, which doesn't require going through add_partial_fill --
        # construct the audit trail directly instead, matching the state
        # add_partial_fill would have produced for two fills at these
        # prices/quantities (see TestPartialFills for coverage of
        # add_partial_fill's own aggregation logic).
        state = OrderFSMState(
            order_id="rec001",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            status=OrderStatus.FILLED,
            filled_qty=1.0,
            average_fill_price=65000.0,
            first_confirmed_at_ms=1000000,
            last_updated_ms=1000500,
            filled_at_prices=[(64900.0, 0.5), (65100.0, 0.5)],
        )
        fsm = OrderFSM(state)

        # Serialize to dict
        snapshot = fsm.state.to_dict()

        assert snapshot["order_id"] == "rec001"
        assert snapshot["status"] == "filled"
        assert snapshot["filled_qty"] == 1.0
        assert len(snapshot["filled_at_prices"]) == 2
        assert snapshot["retry_count"] == 0

        # Verify snapshot is JSON-serializable
        import json
        json_str = json.dumps(snapshot)
        restored = json.loads(json_str)
        assert restored["order_id"] == "rec001"
