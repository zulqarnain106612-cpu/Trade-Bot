"""
Live Executor FSM Integration — SUPERSEDED, retained but unused.

This was the staging ground for TASK-004-004: an FSM-based replacement for
LiveExecutor's order-placement polling loop, to be swapped into live.py.
That swap has since happened directly — src/execution/live.py constructs its
own OrderManager (GAP-004) and calls place_order_with_fsm in
_place_market_order. Nothing imports this module.

It is therefore a second, divergent copy of the live order path: an edit
made here has no effect on trading, and an edit made only here while
live.py drifts is a silent correctness hazard. Change live.py, not this.

Removal needs explicit operator sign-off because it lives under
src/execution/, so it is flagged rather than deleted here.
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
