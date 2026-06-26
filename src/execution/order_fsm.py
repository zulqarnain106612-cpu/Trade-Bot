"""
Order Finite State Machine — formalized order lifecycle with state transitions.

Authority: Transaction Processing Concepts (Gray & Reuter, 1992)
Pattern: State Machine (Fowler, 2010)

State diagram:
    PENDING --(confirmed)→ FILLING
    PENDING --(timeout)→ TIMEOUT
    PENDING --(error)→ FAILED
    FILLING --(filled)→ FILLED
    FILLING --(partial)→ FILLING  [aggregate]
    FILLING --(cancelled)→ CANCELLED
    FILLING --(timeout)→ TIMEOUT
    FILLED, CANCELLED, TIMEOUT, FAILED = terminal

Transitions are guarded: only valid moves allowed. Invalid moves raise OrderFSMError.

State persistence enables:
  - Resume after network error without re-submitting
  - Partial fill aggregation across retries
  - Audit trail and reconciliation
  - Timeout escalation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Final

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class OrderStatus(Enum):
    """Order lifecycle states."""
    PENDING = "pending"        # Submitted, awaiting confirmation
    FILLING = "filling"        # Confirmed by exchange, possibly partial
    FILLED = "filled"          # Fully filled, terminal
    CANCELLED = "cancelled"    # User/system cancelled, terminal
    TIMEOUT = "timeout"        # Exceeded max wait time, terminal
    FAILED = "failed"          # Permanent error, terminal


class OrderFSMError(Exception):
    """Order FSM validation error."""


@dataclass
class OrderFSMState:
    """
    Order state snapshot — serializable, resumable.
    
    Attributes:
        order_id: Exchange order ID (ccxt format)
        symbol: Trading pair (e.g., 'BTC/USDT')
        side: 'buy' or 'sell'
        quantity: Requested quantity
        status: Current OrderStatus
        filled_qty: Cumulative filled quantity (0 <= filled_qty <= quantity)
        filled_at_prices: List of (price, qty) for audit trail
        average_fill_price: Weighted average of fills (None if no fills)
        created_at_ms: UNIX timestamp ms (creation time)
        first_confirmed_at_ms: UNIX timestamp ms (first exchange confirmation)
        last_updated_ms: UNIX timestamp ms (most recent state change)
        last_error: Last exception message (for debugging)
        retry_count: Total retry attempts
        exchange_response: Last raw order dict from exchange (for reconciliation)
    """
    order_id: str
    symbol: str
    side: str  # 'buy' | 'sell'
    quantity: float
    status: OrderStatus
    filled_qty: float = 0.0
    filled_at_prices: list[tuple[float, float]] = field(default_factory=list)
    average_fill_price: float | None = None
    created_at_ms: int = field(default_factory=lambda: int(datetime.now(tz=UTC).timestamp() * 1000))
    first_confirmed_at_ms: int | None = None
    last_updated_ms: int = field(default_factory=lambda: int(datetime.now(tz=UTC).timestamp() * 1000))
    last_error: str = ""
    retry_count: int = 0
    exchange_response: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for storage/serialization."""
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "status": self.status.value,
            "filled_qty": self.filled_qty,
            "filled_at_prices": self.filled_at_prices,
            "average_fill_price": self.average_fill_price,
            "created_at_ms": self.created_at_ms,
            "first_confirmed_at_ms": self.first_confirmed_at_ms,
            "last_updated_ms": self.last_updated_ms,
            "last_error": self.last_error,
            "retry_count": self.retry_count,
            "exchange_response": self.exchange_response,
        }

    def is_terminal(self) -> bool:
        """Check if order is in terminal state (immutable)."""
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.TIMEOUT,
            OrderStatus.FAILED,
        )

    def is_active(self) -> bool:
        """Check if order is still pending/filling."""
        return self.status in (OrderStatus.PENDING, OrderStatus.FILLING)

    def fill_percentage(self) -> float:
        """Return filled / requested as percentage (0.0 to 1.0)."""
        return (self.filled_qty / self.quantity) if self.quantity > 0 else 0.0

    def elapsed_ms(self) -> int:
        """Milliseconds since order creation."""
        return int(datetime.now(tz=UTC).timestamp() * 1000) - self.created_at_ms


class OrderFSM:
    """
    Finite State Machine for order lifecycle.
    
    Rules:
      - Only valid transitions allowed (guarded)
      - Terminal states are immutable
      - Partial fills aggregate across retries
      - Network errors do not change state (retry same state)
      - Timeout transition escalates PENDING→TIMEOUT or FILLING→TIMEOUT
    """

    # Valid transitions: current_status -> list of valid next statuses
    _VALID_TRANSITIONS: Final[dict[OrderStatus, set[OrderStatus]]] = {
        OrderStatus.PENDING: {OrderStatus.FILLING, OrderStatus.TIMEOUT, OrderStatus.FAILED},
        OrderStatus.FILLING: {OrderStatus.FILLING, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.TIMEOUT, OrderStatus.FAILED},  # FILLING self-transition = partial fill aggregation
        OrderStatus.FILLED: set(),      # Terminal
        OrderStatus.CANCELLED: set(),   # Terminal
        OrderStatus.TIMEOUT: set(),     # Terminal
        OrderStatus.FAILED: set(),      # Terminal
    }

    def __init__(self, state: OrderFSMState):
        """Initialize FSM with initial state."""
        self._state = state
        self._log = structlog.get_logger(__name__)

    @property
    def state(self) -> OrderFSMState:
        """Immutable read of current state."""
        return self._state

    def transition(self, next_status: OrderStatus, context: dict[str, Any] | None = None) -> None:
        """
        Attempt transition to next_status.
        
        Args:
            next_status: Target OrderStatus
            context: Optional dict with:
                - filled_qty: Updated filled quantity (for FILLING→FILLED)
                - average_price: Updated average fill price
                - exchange_response: Latest order dict from exchange
                - error: Error message (for FAILED)
        
        Raises:
            OrderFSMError: If transition is invalid or state is already terminal
        """
        context = context or {}

        # Terminal states are immutable
        if self._state.is_terminal():
            raise OrderFSMError(
                f"Cannot transition from terminal state {self._state.status.value} "
                f"to {next_status.value}"
            )

        # Validate transition
        valid_next = self._VALID_TRANSITIONS.get(self._state.status, set())
        if next_status not in valid_next:
            raise OrderFSMError(
                f"Invalid transition: {self._state.status.value} → {next_status.value}. "
                f"Valid next states: {[s.value for s in valid_next]}"
            )

        # Perform transition-specific logic
        if next_status == OrderStatus.FILLING:
            self._transition_to_filling(context)
        elif next_status == OrderStatus.FILLED:
            self._transition_to_filled(context)
        elif next_status == OrderStatus.CANCELLED:
            self._transition_to_cancelled(context)
        elif next_status == OrderStatus.TIMEOUT:
            self._transition_to_timeout(context)
        elif next_status == OrderStatus.FAILED:
            self._transition_to_failed(context)

        # Update status and timestamps
        self._state.status = next_status
        self._state.last_updated_ms = int(datetime.now(tz=UTC).timestamp() * 1000)

    def add_partial_fill(self, qty: float, price: float) -> None:
        """
        Record a partial fill and update average fill price.
        Only valid when in FILLING state.
        
        Args:
            qty: Filled quantity
            price: Fill price
        
        Raises:
            OrderFSMError: If not in FILLING state
        """
        if self._state.status != OrderStatus.FILLING:
            raise OrderFSMError(
                f"Cannot add partial fill: order is in {self._state.status.value} state, "
                f"expected {OrderStatus.FILLING.value}"
            )

        if qty <= 0:
            raise OrderFSMError(f"Partial fill qty must be > 0, got {qty}")

        if price <= 0:
            raise OrderFSMError(f"Fill price must be > 0, got {price}")

        # Aggregate filled quantity
        old_filled = self._state.filled_qty
        self._state.filled_qty += qty

        # Guard against overfill
        if self._state.filled_qty > self._state.quantity:
            self._state.filled_qty = old_filled
            raise OrderFSMError(
                f"Partial fill would exceed order quantity: {old_filled} + {qty} > {self._state.quantity}"
            )

        # Update filled prices list and recalculate average
        self._state.filled_at_prices.append((price, qty))
        self._state.average_fill_price = self._calculate_vwap()
        self._state.last_updated_ms = int(datetime.now(tz=UTC).timestamp() * 1000)

    def _calculate_vwap(self) -> float:
        """Calculate volume-weighted average price from fills."""
        if not self._state.filled_at_prices:
            return 0.0

        total_value = sum(price * qty for price, qty in self._state.filled_at_prices)
        total_qty = sum(qty for _, qty in self._state.filled_at_prices)

        return total_value / total_qty if total_qty > 0 else 0.0

    def _transition_to_filling(self, context: dict[str, Any]) -> None:
        """PENDING → FILLING: Order confirmed by exchange."""
        if self._state.first_confirmed_at_ms is None:
            self._state.first_confirmed_at_ms = int(datetime.now(tz=UTC).timestamp() * 1000)

        if "exchange_response" in context:
            self._state.exchange_response = context["exchange_response"]

    def _transition_to_filled(self, context: dict[str, Any]) -> None:
        """FILLING → FILLED: Order fully executed."""
        if "filled_qty" in context:
            qty = context["filled_qty"]
            if qty < 0 or qty > self._state.quantity:
                raise OrderFSMError(f"Invalid filled qty: {qty}")
            self._state.filled_qty = qty

        if "average_price" in context:
            self._state.average_fill_price = context["average_price"]

        if "exchange_response" in context:
            self._state.exchange_response = context["exchange_response"]

    def _transition_to_cancelled(self, context: dict[str, Any]) -> None:
        """FILLING → CANCELLED: User/system cancelled."""
        if "exchange_response" in context:
            self._state.exchange_response = context["exchange_response"]

    def _transition_to_timeout(self, context: dict[str, Any]) -> None:
        """PENDING/FILLING → TIMEOUT: Exceeded max wait time."""
        self._state.last_error = f"Order timeout after {self._state.elapsed_ms()}ms"
        if "exchange_response" in context:
            self._state.exchange_response = context["exchange_response"]

    def _transition_to_failed(self, context: dict[str, Any]) -> None:
        """Any → FAILED: Permanent error."""
        if "error" in context:
            self._state.last_error = str(context["error"])

    def increment_retry(self) -> None:
        """Increment retry counter (does not change state)."""
        self._state.retry_count += 1
