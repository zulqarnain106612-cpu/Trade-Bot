"""
Live execution via CCXT — Binance and OKX.
Guards: paper_mode flag must be explicitly False to reach this layer.
All orders are limit orders (PostOnly where supported) to minimize fees.
Falls back to market order if PostOnly is rejected.
"""
from __future__ import annotations
import asyncio
import ccxt.async_support as ccxt_async
import structlog

log = structlog.get_logger()

class LiveExecutor:
    def __init__(
        self,
        exchange_id:  str,
        api_key:      str,
        api_secret:   str,
        passphrase:   str  = "",
        paper_mode:   bool = True,
    ):
        self._paper = paper_mode
        if paper_mode:
            log.warning("LiveExecutor instantiated in paper_mode=True — no real orders will be sent")

        params: dict = {"enableRateLimit": True}
        if api_key:
            params["apiKey"] = api_key
            params["secret"] = api_secret
        if passphrase:
            params["password"] = passphrase

        cls = getattr(ccxt_async, exchange_id)
        self._exchange: ccxt_async.Exchange = cls(params)

    async def close(self):
        await self._exchange.close()

    async def place_order(
        self,
        symbol:    str,
        side:      str,
        qty:       float,
        price:     float,
    ) -> dict:
        if self._paper:
            log.info("paper guard — order not sent", symbol=symbol, side=side, qty=qty, price=price)
            return {"id": "paper", "status": "simulated"}

        # Try PostOnly limit first
        try:
            order = await self._exchange.create_order(
                symbol, "limit", side, qty, price,
                params={"postOnly": True},
            )
            log.info("limit postonly placed", order_id=order["id"])
            return order
        except ccxt_async.ExchangeError as e:
            if "would match" in str(e).lower() or "post only" in str(e).lower():
                log.warning("PostOnly rejected — falling back to market", error=str(e))
                order = await self._exchange.create_market_order(symbol, side, qty)
                log.info("market order placed", order_id=order["id"])
                return order
            raise

    async def cancel_order(self, order_id: str, symbol: str) -> dict:
        if self._paper:
            return {"id": order_id, "status": "cancelled_simulated"}
        return await self._exchange.cancel_order(order_id, symbol)

    async def fetch_balance(self) -> dict:
        if self._paper:
            return {}
        return await self._exchange.fetch_balance()

