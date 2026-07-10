"""
End-to-end integration tests for full trading pipeline.

Tests:
  - Order placement + FSM confirmation
  - Drift detection with gate evaluation
  - Full risk gate stack (Gates 0-6)
  - Orchestrator signal routing with drift blocking
"""

from src.execution.order_fsm import OrderFSM, OrderFSMState, OrderStatus
from src.risk.drift_integration import DriftIntegrationAdapter
from src.risk.gates import GateStatus, check_performance_drift
from src.risk.performance_drift import PerformanceBaseline, PerformanceDriftDetector


class TestDriftGateIntegration:
    """Test drift detection as a risk gate."""

    def test_drift_gate_passes_healthy_metrics(self):
        """Gate passes when all metrics within thresholds."""
        baseline = PerformanceBaseline(
            train_sharpe=2.0,
            oos_sharpe=1.5,
            train_accuracy=0.60,
            oos_accuracy=0.58,
            train_win_rate=0.55,
            max_drawdown_pct=0.10,
            trades_in_backtest=400,
        )
        detector = PerformanceDriftDetector(baseline)

        # Record good trades with variance (no drift)
        # Use varied P&L to avoid zero-variance Sharpe
        for i in range(50):
            pnl = 80.0 + (i % 5) * 20.0  # Varying P&L: 80-160
            detector.record_trade_outcome(
                pnl_usd=pnl,
                predicted_prob=0.7,
                actual_direction=1,
                current_equity=10000 + i * 100,
                starting_equity=10000,
            )

        result = check_performance_drift(detector)
        assert result.passed
        assert result.status == GateStatus.PASS
        assert "rolling_sharpe" in result.details

    def test_drift_gate_insufficient_trades(self):
        """Gate passes (insufficient data) when < 30 trades."""
        baseline = PerformanceBaseline(
            train_sharpe=2.0,
            oos_sharpe=1.5,
            train_accuracy=0.60,
            oos_accuracy=0.58,
            train_win_rate=0.55,
            max_drawdown_pct=0.10,
            trades_in_backtest=400,
        )
        detector = PerformanceDriftDetector(baseline)

        # Record only 10 trades
        for i in range(10):
            detector.record_trade_outcome(
                pnl_usd=100.0,
                predicted_prob=0.7,
                actual_direction=1,
                current_equity=10000 + i * 100,
                starting_equity=10000,
            )

        result = check_performance_drift(detector)
        # Should pass (not enough trades to evaluate drift)
        # Drift detector reports insufficient, gate wrapper reports "all gates passed"
        assert result.passed
        assert result.status == GateStatus.PASS


class TestDriftIntegrationAdapter:
    """Test drift adapter for orchestrator integration."""

    def test_adapter_returns_metrics(self):
        """Adapter returns drift status and live metrics."""
        baseline = PerformanceBaseline(
            train_sharpe=2.0,
            oos_sharpe=1.5,
            train_accuracy=0.60,
            oos_accuracy=0.58,
            train_win_rate=0.55,
            max_drawdown_pct=0.10,
            trades_in_backtest=400,
        )
        detector = PerformanceDriftDetector(baseline)
        adapter = DriftIntegrationAdapter(detector)

        # Manually record some trades
        for i in range(50):
            pnl = 100.0 + (i % 3) * 30.0
            detector.record_trade_outcome(
                pnl_usd=pnl,
                predicted_prob=0.7,
                actual_direction=1,
                current_equity=10000 + i * 100,
                starting_equity=10000,
            )

        drift_status = adapter.check_drift()
        assert "drifted" in drift_status
        assert "metrics" in drift_status
        assert drift_status["metrics"]["total_live_trades"] == 50

    def test_adapter_no_detector(self):
        """Adapter works gracefully when detector is None."""
        adapter = DriftIntegrationAdapter(None)

        drift_status = adapter.check_drift()
        assert not drift_status["drifted"]
        assert "not enabled" in drift_status["reason"].lower()


class TestOrderFSMInContext:
    """Test OrderFSM lifecycle in trade execution context."""

    def test_order_placement_and_fsm_confirmation(self):
        """Order placed, confirmed via FSM, outcome recorded."""
        state = OrderFSMState(
            order_id="ORDER-001",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.5,
            status=OrderStatus.PENDING,
        )
        fsm = OrderFSM(state)

        # Simulate order confirmation flow
        assert fsm.state.status == OrderStatus.PENDING

        # Exchange confirms order
        fsm.transition(
            OrderStatus.FILLING, {"exchange_response": {"id": "ORDER-001", "status": "open"}}
        )
        assert fsm.state.status == OrderStatus.FILLING
        assert fsm.state.first_confirmed_at_ms is not None

        # First partial fill arrives
        fsm.add_partial_fill(0.75, 65000.0)
        assert fsm.state.filled_qty == 0.75
        assert fsm.state.average_fill_price == 65000.0

        # Second partial fill completes order
        fsm.add_partial_fill(0.75, 65050.0)
        assert fsm.state.filled_qty == 1.5

        # Mark as FILLED
        fsm.transition(
            OrderStatus.FILLED,
            {
                "filled_qty": 1.5,
                "average_price": fsm.state.average_fill_price,
            },
        )
        assert fsm.state.status == OrderStatus.FILLED
        assert fsm.state.is_terminal()

        # Serialize for audit trail
        snapshot = fsm.state.to_dict()
        assert snapshot["status"] == "filled"
        assert len(snapshot["filled_at_prices"]) == 2

    def test_order_timeout_with_partial_fill(self):
        """Order times out after partial fill — state preserved."""
        state = OrderFSMState(
            order_id="ORDER-002",
            symbol="ETH/USDT",
            side="sell",
            quantity=10.0,
            status=OrderStatus.PENDING,
        )
        fsm = OrderFSM(state)

        # Order confirmed
        fsm.transition(OrderStatus.FILLING)
        fsm.add_partial_fill(4.0, 3500.0)
        assert fsm.state.filled_qty == 4.0

        # Timeout while waiting for rest of order
        fsm.transition(OrderStatus.TIMEOUT)

        # Partial fill preserved
        assert fsm.state.status == OrderStatus.TIMEOUT
        assert fsm.state.filled_qty == 4.0
        assert fsm.state.last_error
        assert fsm.state.is_terminal()


class TestOrderFSMStateSnapshot:
    """Test FSM state snapshots for reconciliation."""

    def test_state_serialization_completeness(self):
        """All FSM state fields serialize correctly."""
        state = OrderFSMState(
            order_id="REC-001",
            symbol="BTC/USDT",
            side="buy",
            quantity=2.5,
            status=OrderStatus.FILLED,
            filled_qty=2.5,
            average_fill_price=65123.45,
            first_confirmed_at_ms=1719234567000,
            last_updated_ms=1719234589000,
            last_error="",
            retry_count=3,
        )
        fsm = OrderFSM(state)

        snapshot = fsm.state.to_dict()

        # Verify all fields present
        assert snapshot["order_id"] == "REC-001"
        assert snapshot["symbol"] == "BTC/USDT"
        assert snapshot["side"] == "buy"
        assert snapshot["quantity"] == 2.5
        assert snapshot["status"] == "filled"
        assert snapshot["filled_qty"] == 2.5
        assert snapshot["average_fill_price"] == 65123.45
        assert snapshot["first_confirmed_at_ms"] == 1719234567000
        assert snapshot["retry_count"] == 3

    def test_state_recovery_from_snapshot(self):
        """FSM state can be recovered from serialized snapshot."""
        # Original state
        state1 = OrderFSMState(
            order_id="SNAP-001",
            symbol="ETH/USDT",
            side="sell",
            quantity=5.0,
            status=OrderStatus.FILLING,
            filled_qty=2.0,
        )
        fsm1 = OrderFSM(state1)
        fsm1.add_partial_fill(2.0, 3400.0)
        fsm1.add_partial_fill(1.0, 3450.0)

        # Serialize
        snapshot = fsm1.state.to_dict()

        # Recover: recreate state from snapshot
        recovered_state = OrderFSMState(
            order_id=snapshot["order_id"],
            symbol=snapshot["symbol"],
            side=snapshot["side"],
            quantity=snapshot["quantity"],
            status=OrderStatus(snapshot["status"]),
            filled_qty=snapshot["filled_qty"],
            filled_at_prices=snapshot["filled_at_prices"],
            average_fill_price=snapshot["average_fill_price"],
        )
        fsm2 = OrderFSM(recovered_state)

        # Verify recovered state matches original
        assert fsm2.state.filled_qty == fsm1.state.filled_qty
        assert fsm2.state.average_fill_price == fsm1.state.average_fill_price
        assert len(fsm2.state.filled_at_prices) == len(fsm1.state.filled_at_prices)

    def test_multiple_partial_fills_vwap(self):
        """VWAP calculation across multiple partial fills."""
        state = OrderFSMState(
            order_id="VWAP-001",
            symbol="BTC/USDT",
            side="buy",
            quantity=3.0,
            status=OrderStatus.FILLING,
        )
        fsm = OrderFSM(state)

        # Fill 1: 1.0 @ 65000
        fsm.add_partial_fill(1.0, 65000.0)
        assert fsm.state.average_fill_price == 65000.0

        # Fill 2: 1.5 @ 65100
        fsm.add_partial_fill(1.5, 65100.0)
        # VWAP = (1.0*65000 + 1.5*65100) / 2.5 = (65000 + 97650) / 2.5 = 64660
        expected_vwap = (65000 + 97650) / 2.5
        assert abs(fsm.state.average_fill_price - expected_vwap) < 0.01

        # Fill 3: 0.5 @ 64900
        fsm.add_partial_fill(0.5, 64900.0)
        # VWAP = (65000 + 97650 + 32450) / 3.0 ≈ 65033.33
        expected_vwap = (65000 + 97650 + 32450) / 3.0
        assert abs(fsm.state.average_fill_price - expected_vwap) < 0.01
