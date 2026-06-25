"""Test suite for Order FSM state machine."""

from datetime import UTC, datetime

import pytest

from src.execution.order_fsm import OrderFSM, OrderFSMError, OrderFSMState, OrderStatus


class TestOrderFSMBasics:
    """Basic FSM construction and state queries."""
    
    def test_create_initial_pending_state(self):
        """FSM starts in PENDING state."""
        state = OrderFSMState(
            order_id="1001",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.5,
            status=OrderStatus.PENDING,
        )
        fsm = OrderFSM(state)
        assert fsm.state.status == OrderStatus.PENDING
        assert fsm.state.filled_qty == 0.0
        assert fsm.state.is_active()
        assert not fsm.state.is_terminal()
    
    def test_state_immutability(self):
        """State is immutable via property."""
        state = OrderFSMState(
            order_id="1002",
            symbol="ETH/USDT",
            side="sell",
            quantity=10.0,
            status=OrderStatus.PENDING,
        )
        fsm = OrderFSM(state)
        returned_state = fsm.state
        assert returned_state is state  # Same reference


class TestOrderFSMTransitions:
    """Test valid and invalid state transitions."""
    
    def test_pending_to_filling(self):
        """PENDING → FILLING: valid transition."""
        state = OrderFSMState(
            order_id="2001",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            status=OrderStatus.PENDING,
        )
        fsm = OrderFSM(state)
        fsm.transition(OrderStatus.FILLING, {"exchange_response": {"id": "2001", "status": "open"}})
        
        assert fsm.state.status == OrderStatus.FILLING
        assert fsm.state.first_confirmed_at_ms is not None
    
    def test_filling_to_filled(self):
        """FILLING → FILLED: valid transition with full fill."""
        state = OrderFSMState(
            order_id="2002",
            symbol="BTC/USDT",
            side="buy",
            quantity=2.0,
            status=OrderStatus.FILLING,
            first_confirmed_at_ms=int(datetime.now(tz=UTC).timestamp() * 1000),
        )
        fsm = OrderFSM(state)
        fsm.transition(
            OrderStatus.FILLED,
            {
                "filled_qty": 2.0,
                "average_price": 65000.0,
                "exchange_response": {"id": "2002", "status": "closed", "filled": 2.0},
            },
        )
        
        assert fsm.state.status == OrderStatus.FILLED
        assert fsm.state.filled_qty == 2.0
        assert fsm.state.average_fill_price == 65000.0
        assert fsm.state.is_terminal()
    
    def test_filling_to_cancelled(self):
        """FILLING → CANCELLED: user cancellation."""
        state = OrderFSMState(
            order_id="2003",
            symbol="BTC/USDT",
            side="sell",
            quantity=1.5,
            status=OrderStatus.FILLING,
            first_confirmed_at_ms=int(datetime.now(tz=UTC).timestamp() * 1000),
        )
        fsm = OrderFSM(state)
        fsm.transition(
            OrderStatus.CANCELLED,
            {"exchange_response": {"id": "2003", "status": "cancelled"}},
        )
        
        assert fsm.state.status == OrderStatus.CANCELLED
        assert fsm.state.is_terminal()
    
    def test_pending_to_timeout(self):
        """PENDING → TIMEOUT: order confirmation timeout."""
        state = OrderFSMState(
            order_id="2004",
            symbol="ETH/USDT",
            side="buy",
            quantity=5.0,
            status=OrderStatus.PENDING,
        )
        fsm = OrderFSM(state)
        fsm.transition(OrderStatus.TIMEOUT)
        
        assert fsm.state.status == OrderStatus.TIMEOUT
        assert fsm.state.last_error  # Should have timeout message
        assert fsm.state.is_terminal()
    
    def test_filling_to_timeout(self):
        """FILLING → TIMEOUT: partial fill timeout."""
        state = OrderFSMState(
            order_id="2005",
            symbol="BTC/USDT",
            side="buy",
            quantity=3.0,
            status=OrderStatus.FILLING,
            filled_qty=1.0,
            first_confirmed_at_ms=int(datetime.now(tz=UTC).timestamp() * 1000),
        )
        fsm = OrderFSM(state)
        fsm.transition(OrderStatus.TIMEOUT)
        
        assert fsm.state.status == OrderStatus.TIMEOUT
        assert fsm.state.filled_qty == 1.0  # Partial fill preserved
    
    def test_invalid_transition_from_terminal_filled(self):
        """Cannot transition from FILLED (terminal)."""
        state = OrderFSMState(
            order_id="3001",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            status=OrderStatus.FILLED,
        )
        fsm = OrderFSM(state)
        
        with pytest.raises(OrderFSMError, match="Cannot transition from terminal state"):
            fsm.transition(OrderStatus.CANCELLED)
    
    def test_invalid_transition_pending_to_filled(self):
        """Cannot skip FILLING: PENDING → FILLED invalid."""
        state = OrderFSMState(
            order_id="3002",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            status=OrderStatus.PENDING,
        )
        fsm = OrderFSM(state)
        
        with pytest.raises(OrderFSMError, match="Invalid transition"):
            fsm.transition(OrderStatus.FILLED)
    
    def test_invalid_transition_filled_to_cancelled(self):
        """Cannot transition FILLED → CANCELLED (already terminal)."""
        state = OrderFSMState(
            order_id="3003",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            status=OrderStatus.FILLED,
        )
        fsm = OrderFSM(state)
        
        with pytest.raises(OrderFSMError):
            fsm.transition(OrderStatus.CANCELLED)


class TestPartialFills:
    """Test partial fill aggregation and VWAP calculation."""
    
    def test_add_partial_fill_single(self):
        """Add single partial fill to FILLING order."""
        state = OrderFSMState(
            order_id="4001",
            symbol="BTC/USDT",
            side="buy",
            quantity=3.0,
            status=OrderStatus.FILLING,
            first_confirmed_at_ms=int(datetime.now(tz=UTC).timestamp() * 1000),
        )
        fsm = OrderFSM(state)
        fsm.add_partial_fill(1.0, 65000.0)
        
        assert fsm.state.filled_qty == 1.0
        assert fsm.state.average_fill_price == 65000.0
        assert len(fsm.state.filled_at_prices) == 1
    
    def test_add_partial_fill_multiple_vwap(self):
        """Add multiple partial fills and verify VWAP calculation."""
        state = OrderFSMState(
            order_id="4002",
            symbol="BTC/USDT",
            side="buy",
            quantity=3.0,
            status=OrderStatus.FILLING,
            first_confirmed_at_ms=int(datetime.now(tz=UTC).timestamp() * 1000),
        )
        fsm = OrderFSM(state)
        
        # Fill 1: 1.0 @ 65000
        fsm.add_partial_fill(1.0, 65000.0)
        # Fill 2: 1.5 @ 65100
        fsm.add_partial_fill(1.5, 65100.0)
        # Fill 3: 0.5 @ 64900
        fsm.add_partial_fill(0.5, 64900.0)
        
        assert fsm.state.filled_qty == 3.0
        # VWAP = (1.0*65000 + 1.5*65100 + 0.5*64900) / 3.0
        #      = (65000 + 97650 + 32450) / 3.0 = 195100 / 3.0 ≈ 65033.33
        expected_vwap = (65000 + 97650 + 32450) / 3.0
        assert abs(fsm.state.average_fill_price - expected_vwap) < 0.01
    
    def test_add_partial_fill_exceeds_quantity(self):
        """Cannot add fills that exceed order quantity."""
        state = OrderFSMState(
            order_id="4003",
            symbol="BTC/USDT",
            side="buy",
            quantity=2.0,
            status=OrderStatus.FILLING,
            first_confirmed_at_ms=int(datetime.now(tz=UTC).timestamp() * 1000),
            filled_qty=1.5,
        )
        fsm = OrderFSM(state)
        
        # Try to add 1.0, but already have 1.5, would exceed 2.0
        with pytest.raises(OrderFSMError, match="would exceed order quantity"):
            fsm.add_partial_fill(1.0, 65000.0)
    
    def test_add_partial_fill_invalid_state(self):
        """Cannot add partial fills to non-FILLING states."""
        state = OrderFSMState(
            order_id="4004",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            status=OrderStatus.PENDING,
        )
        fsm = OrderFSM(state)
        
        with pytest.raises(OrderFSMError, match="Cannot add partial fill"):
            fsm.add_partial_fill(0.5, 65000.0)
    
    def test_fill_percentage(self):
        """Check fill_percentage() calculation."""
        state = OrderFSMState(
            order_id="4005",
            symbol="BTC/USDT",
            side="buy",
            quantity=4.0,
            status=OrderStatus.FILLING,
            filled_qty=1.0,
            first_confirmed_at_ms=int(datetime.now(tz=UTC).timestamp() * 1000),
        )
        fsm = OrderFSM(state)
        
        assert fsm.state.fill_percentage() == 0.25
        
        fsm.add_partial_fill(2.0, 65000.0)
        assert fsm.state.fill_percentage() == 0.75


class TestRetryCounter:
    """Test retry tracking."""
    
    def test_increment_retry(self):
        """Increment retry counter without changing state."""
        state = OrderFSMState(
            order_id="5001",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            status=OrderStatus.PENDING,
        )
        fsm = OrderFSM(state)
        
        assert fsm.state.retry_count == 0
        fsm.increment_retry()
        assert fsm.state.retry_count == 1
        assert fsm.state.status == OrderStatus.PENDING  # Status unchanged
        
        fsm.increment_retry()
        fsm.increment_retry()
        assert fsm.state.retry_count == 3
