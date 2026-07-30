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
    ) -> tuple[OrderFSM, dict[str, Any]]:
        """
        Place a market order and track via FSM.

        Replaces _place_market_order polling loop with state machine.

        Returns:
            (OrderFSM, final_order_dict from exchange)

        Raises:
            asyncio.TimeoutError: Order confirmation timeout
            ccxt.ExchangeError: Permanent exchange error
        """
        exchange = self._fetcher.get_order_exchange()

        try:
            fsm, order = await self._order_manager.place_order_with_fsm(
                exchange=exchange,
                symbol=symbol,
                side=side,
                quantity=quantity,
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
