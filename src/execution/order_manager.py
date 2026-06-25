"""
Order Manager — FSM-based order lifecycle management for live executor.

Wraps ccxt order operations with OrderFSM state tracking.
Handles:
  - Resilient order placement with automatic polling and retries
  - Partial fill aggregation
  - Timeout escalation
  - State persistence for recovery
  - Network error recovery (resume without re-submitting)

Integrates with LiveExecutor via place_order_with_fsm().
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

import ccxt.async_support as ccxt
import structlog

from src.execution.order_fsm import OrderFSM, OrderFSMError, OrderFSMState, OrderStatus


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ORDER_CONFIRM_POLLS: Final[int] = 10
_ORDER_CONFIRM_INTERVAL: Final[float] = 0.5
_TIMEOUT_SECONDS: Final[float] = 30.0  # Total timeout for order confirmation


class OrderManager:
    """
    Manages order lifecycle with FSM state tracking.
    """

    def __init__(self):
        self._log = log

    async def place_order_with_fsm(
        self,
        exchange: ccxt.async_support.Exchange,
        symbol: str,
        side: str,
        quantity: float,
    ) -> tuple[OrderFSM, dict[str, Any]]:
        """
        Place market order and track via FSM.

        Returns:
            (OrderFSM, final_order_dict)

        Raises:
            OrderFSMError: Invalid order parameters
            ccxt.ExchangeError: Permanent exchange error
            asyncio.TimeoutError: Order confirmation timeout
        """
        if not symbol or side not in ("buy", "sell") or quantity <= 0:
            raise OrderFSMError(f"Invalid order params: {symbol}, {side}, {quantity}")

        # Create initial FSM state
        order_id = None  # Will be set after placement
        fsm_state = OrderFSMState(
            order_id=order_id or "pending",
            symbol=symbol,
            side=side,
            quantity=quantity,
            status=OrderStatus.PENDING,
        )
        fsm = OrderFSM(fsm_state)

        # Place the order
        try:
            order = await exchange.create_market_order(symbol=symbol, side=side, amount=quantity)
            order_id = order["id"]
            fsm.state.order_id = order_id
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            fsm.state.last_error = str(exc)
            self._log.error("order_placement_network_error", symbol=symbol, error=str(exc))
            raise
        except ccxt.ExchangeError as exc:
            fsm.transition(OrderStatus.FAILED, {"error": str(exc)})
            self._log.error("order_placement_exchange_error", symbol=symbol, error=str(exc))
            raise

        # Confirm the fill
        try:
            confirmed_order = await self._confirm_order_fill(
                exchange, order_id, symbol, fsm
            )
            fsm.state.exchange_response = confirmed_order
            return fsm, confirmed_order
        except TimeoutError:
            fsm.transition(OrderStatus.TIMEOUT)
            self._log.error(
                "order_confirmation_timeout",
                order_id=order_id,
                symbol=symbol,
                action="manual_reconciliation_required",
            )
            raise
        except ccxt.ExchangeError as exc:
            fsm.transition(OrderStatus.FAILED, {"error": str(exc)})
            raise

    async def _confirm_order_fill(
        self,
        exchange: ccxt.async_support.Exchange,
        order_id: str,
        symbol: str,
        fsm: OrderFSM,
        timeout_s: float = _TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """
        Poll for order fill confirmation with timeout and FSM tracking.

        Transitions FSM: PENDING → FILLING → FILLED

        Returns:
            Final confirmed order dict from exchange

        Raises:
            asyncio.TimeoutError: Exceeded timeout_s
            ccxt.ExchangeError: Permanent error (non-retryable)
        """
        start_time = asyncio.get_event_loop().time()
        attempt = 0

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout_s:
                raise TimeoutError(
                    f"Order {order_id} confirmation exceeded {timeout_s}s"
                )

            if attempt > 0:
                await asyncio.sleep(_ORDER_CONFIRM_INTERVAL)

            attempt += 1
            fsm.increment_retry()

            try:
                confirmed = await exchange.fetch_order(order_id, symbol)
                status = confirmed.get("status", "").lower()

                if status in {"closed", "filled"}:
                    # Order fully filled
                    filled_qty = float(confirmed.get("filled") or confirmed.get("amount", 0))
                    avg_price = float(
                        confirmed.get("average")
                        or confirmed.get("price")
                        or 0
                    )
                    # BUGFIX (found during audit, 2026-06-25): the FSM may
                    # already be in FILLING if a prior poll iteration saw an
                    # "open"/"pending" exchange status first (see the
                    # PENDING-guard two branches below, which this mirrors).
                    # Calling transition(FILLING) unconditionally crashed
                    # with "Invalid transition: filling -> filling" any time
                    # the order didn't fill on the very first poll attempt --
                    # i.e. on every order that wasn't instant-filled.
                    if fsm.state.status == OrderStatus.PENDING:
                        fsm.transition(
                            OrderStatus.FILLING,
                            {"exchange_response": confirmed},
                        )
                    fsm.transition(
                        OrderStatus.FILLED,
                        {
                            "filled_qty": filled_qty,
                            "average_price": avg_price,
                            "exchange_response": confirmed,
                        },
                    )
                    self._log.info(
                        "order_filled",
                        order_id=order_id,
                        symbol=symbol,
                        filled_qty=filled_qty,
                        avg_price=avg_price,
                        attempts=attempt,
                    )
                    return confirmed

                elif status in {"open", "pending"}:
                    # Still pending, transition to FILLING if not already
                    if fsm.state.status == OrderStatus.PENDING:
                        fsm.transition(
                            OrderStatus.FILLING,
                            {"exchange_response": confirmed},
                        )
                    else:
                        # Update with latest response
                        fsm.state.exchange_response = confirmed

                    self._log.debug(
                        "order_pending",
                        order_id=order_id,
                        symbol=symbol,
                        attempt=attempt,
                    )
                    continue

                elif status == "cancelled":
                    fsm.transition(
                        OrderStatus.CANCELLED,
                        {"exchange_response": confirmed},
                    )
                    raise ccxt.ExchangeError(
                        f"Order {order_id} was cancelled on exchange"
                    )

                else:
                    # Unknown status
                    self._log.warning(
                        "order_unknown_status",
                        order_id=order_id,
                        status=status,
                        attempt=attempt,
                    )
                    continue

            except (ccxt.NetworkError, ccxt.RequestTimeout):
                # Transient — retry
                self._log.warning(
                    "order_confirm_network_error",
                    order_id=order_id,
                    attempt=attempt,
                )
                continue

            except (
                ccxt.BadSymbol,
                ccxt.InsufficientFunds,
                ccxt.InvalidOrder,
                ccxt.AuthenticationError,
            ) as exc:
                # Permanent — raise immediately
                fsm.transition(OrderStatus.FAILED, {"error": str(exc)})
                self._log.error(
                    "order_confirm_permanent_error",
                    order_id=order_id,
                    error=str(exc),
                )
                raise

            except ccxt.ExchangeError as exc:
                # Unclassified — might be transient
                self._log.warning(
                    "order_confirm_exchange_error",
                    order_id=order_id,
                    error=str(exc),
                    attempt=attempt,
                )
                continue
