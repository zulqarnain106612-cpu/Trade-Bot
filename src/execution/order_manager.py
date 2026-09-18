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

from src.execution.exchange_contract import (
    FSM_STATUS,
    TERMINAL_FILLED,
    ExchangeOrderStatus,
    parse_order,
)
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

    def _record_incremental_fill(self, fsm: OrderFSM, confirmed: dict[str, Any]) -> None:
        """
        Feed one poll's newly-filled quantity into the FSM.

        ccxt reports `filled` and `average` cumulatively on an open order,
        while add_partial_fill takes an increment, so the delta is derived
        against what the FSM already holds. The increment's price is backed
        out of the two cumulative VWAPs rather than taken as `average`
        directly -- using the running average as the price of the newest
        piece would bias the FSM's own VWAP toward the earliest fills and
        stop it reproducing the exchange's number.

        Anything unusable (missing fields, no progress, a non-positive
        derived price) is skipped rather than guessed at: this is
        book-keeping alongside the poll, and it must never be the reason a
        live order's confirmation loop dies.
        """
        if fsm.state.status != OrderStatus.FILLING:
            return

        raw_filled = confirmed.get("filled")
        raw_avg = confirmed.get("average")
        if raw_filled is None or raw_avg is None:
            return
        try:
            cumulative_qty = float(raw_filled)
            cumulative_avg = float(raw_avg)
        except (TypeError, ValueError):
            return

        delta_qty = cumulative_qty - fsm.state.filled_qty
        if delta_qty <= 0.0 or cumulative_avg <= 0.0:
            return

        # average_fill_price is None until the first piece is recorded.
        prior_avg = fsm.state.average_fill_price or 0.0
        prior_value = prior_avg * fsm.state.filled_qty
        delta_price = (cumulative_avg * cumulative_qty - prior_value) / delta_qty
        if delta_price <= 0.0:
            return

        try:
            fsm.add_partial_fill(delta_qty, delta_price)
        except OrderFSMError as exc:
            # Overfill against the order's own quantity is the realistic
            # case, and it means the exchange and the FSM disagree about the
            # order -- worth surfacing, not worth aborting the poll over.
            self._log.warning(
                "order_manager.partial_fill_rejected",
                order_id=fsm.state.order_id,
                symbol=fsm.state.symbol,
                delta_qty=delta_qty,
                cumulative_qty=cumulative_qty,
                error=str(exc),
            )

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

        # Bind the key for the life of this submission so every line logged
        # under it -- here, in the router, and in the retry loop -- names the
        # order it belongs to. The key is already the order's stable identity
        # (it is the venue's client order id), so nothing new has to be
        # invented, and it composes with the tick's trace_id: a filled order
        # can be traced back to the signal that asked for it.
        #
        # bound_contextvars rather than clear_contextvars: clearing would take
        # the trace_id the surrounding tick bound. It also restores whatever
        # order_key was bound before instead of deleting the name outright,
        # which is the difference if a submission ever runs inside another.
        with structlog.contextvars.bound_contextvars(order_key=idempotency_key):
            return await self._place_order_with_fsm(
                exchange, symbol, side, quantity, idempotency_key, params
            )

    async def _place_order_with_fsm(
        self,
        exchange: ccxt.async_support.Exchange,
        symbol: str,
        side: str,
        quantity: float,
        idempotency_key: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[OrderFSM, dict[str, Any]]:
        """Body of place_order_with_fsm, run with order_key bound."""

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
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        attempt = 0

        while True:
            elapsed = loop.time() - start_time
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
                # EXEC-003: parse against the declared contract rather than
                # reading fields ad hoc. The contract is total -- every
                # response yields an OrderUpdate -- and it refuses a terminal
                # fill that carries no usable price or quantity, which is the
                # case that would otherwise record a position as acquired for
                # free. See src/execution/exchange_contract.py.
                update = parse_order(confirmed, expected_symbol=symbol, expected_order_id=order_id)

                if update.needs_reconciliation:
                    if update.status in TERMINAL_FILLED:
                        # A venue reporting a completed fill it cannot
                        # substantiate. Fail loudly for manual reconciliation
                        # rather than booking the fill -- matching the
                        # UNTRACKED_POSITION pattern used elsewhere on the
                        # live path for responses that cannot be trusted.
                        self._log.critical(
                            "order_manager.filled_order_unusable_fill",
                            order_id=order_id,
                            symbol=symbol,
                            problems=list(update.problems),
                            exchange_response=confirmed,
                            action="MANUAL_RECONCILIATION_REQUIRED",
                        )
                        fsm.transition(
                            OrderStatus.FAILED,
                            {
                                "error": "filled order missing fill price or quantity",
                                "problems": list(update.problems),
                                "exchange_response": confirmed,
                            },
                        )
                        raise ValueError(
                            f"Order {order_id} reported {update.raw_status!r} but the "
                            f"exchange response {update.reason} -- cannot safely "
                            "record the fill."
                        )

                    # INV-003: an unrecognised status keeps polling. It never
                    # becomes FILLED, and the loop's own timeout is what ends
                    # it -- deliberately, because "the venue said something we
                    # do not understand" is not evidence the order failed.
                    self._log.warning(
                        "order_unknown_status",
                        order_id=order_id,
                        status=update.raw_status,
                        problems=list(update.problems),
                        attempt=attempt,
                    )
                    continue

                if update.status in TERMINAL_FILLED:
                    filled_qty = float(update.filled_qty)
                    avg_price = float(update.average_price)
                    # The FSM may already be in FILLING if a prior poll saw an
                    # open status first; transitioning unconditionally would
                    # raise "Invalid transition: filling -> filling" on every
                    # order that did not fill on the first attempt.
                    if fsm.state.status == OrderStatus.PENDING:
                        fsm.transition(OrderStatus.FILLING, {"exchange_response": confirmed})
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

                if update.status is ExchangeOrderStatus.OPEN:
                    if fsm.state.status == OrderStatus.PENDING:
                        fsm.transition(OrderStatus.FILLING, {"exchange_response": confirmed})
                    else:
                        fsm.state.exchange_response = confirmed

                    # OrderFSM has carried add_partial_fill/_calculate_vwap/
                    # fill_percentage since it was written, and nothing ever
                    # called them: the poll loop only ever looked at terminal
                    # statuses, so an order that filled in pieces reported
                    # filled_qty=0 for its whole life then jumped to FILLED.
                    self._record_incremental_fill(fsm, confirmed)
                    self._log.debug(
                        "order_pending", order_id=order_id, symbol=symbol, attempt=attempt
                    )
                    continue

                # Every remaining recognised status ends the order without a
                # fill: cancelled, rejected, expired. The FSM only allows
                # CANCELLED from FILLING, and a market order rejected
                # instantly can report it on the very first poll while still
                # PENDING, so mirror the guard the branches above use.
                if fsm.state.status == OrderStatus.PENDING:
                    fsm.transition(OrderStatus.FILLING, {"exchange_response": confirmed})
                fsm.transition(FSM_STATUS[update.status], {"exchange_response": confirmed})
                # Do not raise here -- see the cancelled_error comment above.
                cancelled_error = ccxt.ExchangeError(
                    f"Order {order_id} was cancelled on exchange (reported {update.raw_status!r})"
                )

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
