"""
Live Executor FSM Integration — refactored order placement with OrderFSM.

Replaces polling loop in LiveExecutor._place_market_order with
OrderManager.place_order_with_fsm for state machine driven order handling.

This module provides the replacement implementation that can be swapped
into live.py via refactoring TASK-004-004.
"""

from __future__ import annotations

from typing import Any

import ccxt.async_support as ccxt
import structlog

from src.execution.idempotency import derive_idempotency_key
from src.execution.order_fsm import OrderFSM
from src.execution.order_manager import OrderManager


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class LiveExecutorOrderFSM:
    """
    Wrapper providing FSM-based order placement for LiveExecutor.

    Usage in live.py:
        self._order_manager = OrderManager()

        # Replace: order = await self._place_market_order(...)
        # With: fsm = await self._place_market_order_with_fsm(...)
        fsm, order_dict = await self._place_market_order_with_fsm(
            symbol=symbol,
            side=side,
            quantity=quantity,
            purpose="entry",
        )
    """

    def __init__(self, fetcher: Any):
        """Initialize with market data fetcher for exchange access."""
        self._fetcher = fetcher
        self._order_manager = OrderManager()
        self._log = structlog.get_logger(__name__)

    async def place_market_order_with_fsm(
        self,
        symbol: str,
        side: str,
        quantity: float,
        *,
        purpose: str,
        intent_id: str | None = None,
        strategy_id: str = "live_executor",
    ) -> tuple[OrderFSM, dict[str, Any]]:
        """
        Place a market order and track via FSM.

        Replaces _place_market_order polling loop with state machine.

        ``purpose`` and ``intent_id`` define the order's identity for LAW3
        de-duplication -- see :func:`derive_idempotency_key`. Mirrors
        LiveExecutor._place_market_order: ``purpose`` is keyword-only and
        required so a new call site cannot inherit a default that silently
        collides with an unrelated order.

        Returns:
            (OrderFSM, final_order_dict from exchange)

        Raises:
            asyncio.TimeoutError: Order confirmation timeout
            ccxt.ExchangeError: Permanent exchange error
        """
        exchange = self._fetcher.get_order_exchange()

        idempotency_key = derive_idempotency_key(
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            purpose=purpose,
            intent_id=intent_id,
        )

        try:
            fsm, order = await self._order_manager.place_order_with_fsm(
                exchange=exchange,
                symbol=symbol,
                side=side,
                quantity=quantity,
                idempotency_key=idempotency_key,
            )

            self._log.info(
                "live.order_placed_with_fsm",
                order_id=fsm.state.order_id,
                symbol=symbol,
                side=side,
                qty=quantity,
                status=fsm.state.status.value,
                filled=fsm.state.filled_qty,
                avg_price=round(fsm.state.average_fill_price or 0, 4),
            )

            return fsm, order

        except TimeoutError as exc:
            self._log.error(
                "live.order_timeout_fsm",
                symbol=symbol,
                side=side,
                qty=quantity,
                error=str(exc),
                exc_info=True,
            )
            raise
        except ccxt.ExchangeError as exc:
            self._log.error(
                "live.order_exchange_error_fsm",
                symbol=symbol,
                side=side,
                qty=quantity,
                error=str(exc),
                exc_info=True,
            )
            raise
