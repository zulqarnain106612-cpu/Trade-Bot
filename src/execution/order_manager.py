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

from src.execution.idempotency import (
    IdempotencyRegistry,
    client_order_id_params,
)
from src.execution.order_fsm import OrderFSM, OrderFSMError, OrderFSMState, OrderStatus


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ORDER_CONFIRM_POLLS: Final[int] = 10
_ORDER_CONFIRM_INTERVAL: Final[float] = 0.5
_TIMEOUT_SECONDS: Final[float] = 30.0  # Total timeout for order confirmation


class OrderManager:
    """
    Manages order lifecycle with FSM state tracking.
    """

    def __init__(self, registry: IdempotencyRegistry | None = None) -> None:
        self._log = log
        # LAW3: one registry per manager instance, shared by every order this
        # manager places. Injectable so an executor can hand in a registry
        # whose lifetime spans reconnects rather than one tied to a manager
        # that gets rebuilt on reconnect -- a registry that dies with the
        # connection cannot detect the reconnect replay it exists to stop.
        self._idempotency: IdempotencyRegistry = registry or IdempotencyRegistry()

    @property
    def idempotency(self) -> IdempotencyRegistry:
        """Registry of idempotency keys seen by this manager."""
        return self._idempotency

    async def place_order_with_fsm(
        self,
        exchange: ccxt.async_support.Exchange,
        symbol: str,
        side: str,
        quantity: float,
        idempotency_key: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[OrderFSM, dict[str, Any]]:
        """
        Place market order and track via FSM.

        ``idempotency_key`` is mandatory (LAW3). It is claimed in the registry
        before the request goes out and attached to the exchange call as the
        venue's client order id, so a duplicate is stopped locally on the fast
        path and by the exchange itself if this process died mid-submit.

        Returns:
            (OrderFSM, final_order_dict)

        Raises:
            OrderFSMError: Invalid order parameters
            DuplicateOrderError: Key already in flight or completed
            ccxt.ExchangeError: Permanent exchange error
            asyncio.TimeoutError: Order confirmation timeout
        """
        if not symbol or side not in ("buy", "sell") or quantity <= 0:
            raise OrderFSMError(f"Invalid order params: {symbol}, {side}, {quantity}")
        if not idempotency_key:
            raise OrderFSMError("idempotency_key is required for order submission (LAW3)")

        # Claim the key before anything can reach the wire. Raises
        # DuplicateOrderError to the caller if this intent was already sent.
        await self._idempotency.reserve(idempotency_key)

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
        fsm.state.idempotency_key = idempotency_key

        order_params = client_order_id_params(
            getattr(exchange, "id", None), idempotency_key, params
        )

        # Place the order
        try:
            order = await exchange.create_market_order(
                symbol=symbol, side=side, amount=quantity, params=order_params
            )
            order_id = order["id"]
            fsm.state.order_id = order_id
            await self._idempotency.complete(idempotency_key, order_id, order)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            # NOT retryable in the idempotency sense: the request may have been
            # executed with only the response lost. Keeping the key claimed
            # forces the order into reconciliation instead of letting a retry
            # place a second one.
            await self._idempotency.fail(idempotency_key, str(exc), retryable=False)
            fsm.state.last_error = str(exc)
            self._log.error(
                "order_placement_network_error",
                symbol=symbol,
                idempotency_key=idempotency_key,
                error=str(exc),
                action="manual_reconciliation_required",
                exc_info=True,
            )
            raise
        except ccxt.ExchangeError as exc:
            # The exchange answered and refused: nothing was placed, so the
            # key is released and the intent may legitimately be retried.
            await self._idempotency.fail(idempotency_key, str(exc), retryable=True)
            fsm.transition(OrderStatus.FAILED, {"error": str(exc)})
            self._log.error(
                "order_placement_exchange_error",
                symbol=symbol,
                idempotency_key=idempotency_key,
                error=str(exc),
                exc_info=True,
            )
            raise

        # Confirm the fill
        try:
            confirmed_order = await self._confirm_order_fill(exchange, order_id, symbol, fsm)
            fsm.state.exchange_response = confirmed_order
            return fsm, confirmed_order
        except TimeoutError:
            fsm.transition(OrderStatus.TIMEOUT)
            self._log.error(
                "order_confirmation_timeout",
                order_id=order_id,
                symbol=symbol,
                action="manual_reconciliation_required",
                exc_info=True,
            )
            raise
        except ccxt.ExchangeError as exc:
            # _confirm_order_fill already transitions permanent-error cases
            # (BadSymbol/InsufficientFunds/InvalidOrder/AuthenticationError)
            # to FAILED itself before re-raising -- transitioning again here
            # unconditionally would hit "Cannot transition from terminal
            # state failed to failed" and mask the real ExchangeError with a
            # confusing OrderFSMError instead. Only transition if the FSM
            # hasn't already reached a terminal state.
            if not fsm.state.is_terminal():
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
                raise TimeoutError(f"Order {order_id} confirmation exceeded {timeout_s}s")

            if attempt > 0:
                await asyncio.sleep(_ORDER_CONFIRM_INTERVAL)

            attempt += 1
            fsm.increment_retry()

            # Cancellation is a definitive terminal outcome, not an exchange
            # error to classify as transient/permanent -- it must bypass this
            # loop's own except clauses below, or a raise from the "cancelled"
            # branch (below) gets caught by this same try's generic
            # `except ccxt.ExchangeError` handler and silently retried until
            # timeout instead of failing immediately.
            cancelled_error: ccxt.ExchangeError | None = None
            try:
                confirmed = await exchange.fetch_order(order_id, symbol)
                status = confirmed.get("status", "").lower()

                if status in {"closed", "filled"}:
                    # Order fully filled
                    filled_qty = float(confirmed.get("filled") or confirmed.get("amount", 0))
                    # UI-009: check each field with `is None`, not `or` --
                    # `confirmed.get("average") or confirmed.get("price")`
                    # would treat an explicit 0.0 fill price exactly like a
                    # missing field (0.0 is falsy) and silently fall through
                    # to the next field / to a 0.0 default. A real fill
                    # price is never legitimately 0.0, so both "missing" and
                    # "explicitly zero" must be rejected the same way.
                    average_field = confirmed.get("average")
                    price_field = confirmed.get("price")
                    raw_avg_price = average_field if average_field is not None else price_field
                    if raw_avg_price is None or float(raw_avg_price) <= 0.0:
                        # An exchange reporting "filled" with no usable fill
                        # price is a malformed/untrustworthy response --
                        # silently defaulting to avg_price=0.0 would corrupt
                        # PnL/notional accounting (a position recorded as
                        # "acquired for free") rather than surfacing the
                        # anomaly. Fail loudly so this gets a manual
                        # reconciliation, matching the UNTRACKED_POSITION
                        # critical-log pattern used elsewhere in the live
                        # path for exchange responses that can't be trusted.
                        self._log.critical(
                            "order_manager.filled_order_missing_fill_price",
                            order_id=order_id,
                            symbol=symbol,
                            exchange_response=confirmed,
                            action="MANUAL_RECONCILIATION_REQUIRED",
                        )
                        # Transition to FAILED here (not left to a caller's
                        # except clause) since ValueError isn't one of the
                        # exception types place_order_with_fsm's caller
                        # already catches to perform this transition.
                        fsm.transition(
                            OrderStatus.FAILED,
                            {
                                "error": "filled order missing fill price",
                                "exchange_response": confirmed,
                            },
                        )
                        raise ValueError(
                            f"Order {order_id} reported {status!r} but exchange "
                            "response has no usable 'average'/'price' field "
                            "-- cannot safely record a fill price."
                        )
                    avg_price = float(raw_avg_price)
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
                    # The FSM only allows CANCELLED from FILLING (not
                    # directly from PENDING) -- a realistic outcome for a
                    # market order rejected/cancelled instantly (no
                    # liquidity, self-trade prevention, IOC-style rejection)
                    # can report "cancelled" on the very first poll while
                    # still PENDING. Mirror the same PENDING-guard the
                    # "filled" and "open"/"pending" branches above already
                    # use, or this raises an unhandled OrderFSMError instead
                    # of properly recording the cancellation.
                    if fsm.state.status == OrderStatus.PENDING:
                        fsm.transition(
                            OrderStatus.FILLING,
                            {"exchange_response": confirmed},
                        )
                    fsm.transition(
                        OrderStatus.CANCELLED,
                        {"exchange_response": confirmed},
                    )
                    # Do not raise here -- see cancelled_error comment above.
                    cancelled_error = ccxt.ExchangeError(
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
                    exc_info=True,
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

            if cancelled_error is not None:
                raise cancelled_error
