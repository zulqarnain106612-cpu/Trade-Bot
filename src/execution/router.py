"""
SmartOrderRouter — Kyle-lambda market-impact aware multi-venue router.

Routes orders across Binance, Bybit, OKX using best-venue selection
(live order book comparison) and algorithm selection based on:
  - horizon index (faster → IOC, slower → TWAP)
  - Kyle lambda market impact estimate
  - order size in USD

Algorithms:
  IOC     — Immediate-or-Cancel (horizon <= 1 AND impact < $5)
  iceberg — Hidden size limit order (horizon <= 4 AND impact < $20)
  TWAP    — Time-Weighted Average Price (all others)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class RouteResult:
    venue: str
    algo: str
    order_id: str | None
    filled_qty: float
    avg_price: float
    fee_usd: float
    slippage_bps: float
    success: bool
    error: str | None = None


class SmartOrderRouter:
    """
    Multi-venue order router with Kyle-lambda market-impact-aware algorithm selection.

    Compares real-time order books across exchanges to find the best venue,
    then executes using IOC, iceberg, or TWAP depending on horizon and impact.
    """

    def __init__(self, exchanges: list[str] | None = None) -> None:
        import ccxt.async_support as ccxt

        # `or` would treat an explicit empty list as "unset" and connect to all
        # three live venues -- the opposite of what disabling every exchange
        # asks for. Only None means "use the defaults".
        self._exchange_names = ["binance", "bybit", "okx"] if exchanges is None else list(exchanges)
        self._exchanges: dict[str, Any] = {}
        for name in self._exchange_names:
            try:
                ex_class = getattr(ccxt, name)
                self._exchanges[name] = ex_class(
                    {
                        "enableRateLimit": True,
                        "options": {"defaultType": "future"},
                    }
                )
            except AttributeError:
                log.warning("exchange_not_found", name=name)

    async def route(
        self,
        signal: dict,
        kyle_lambda: float,
        size_usd: float,
    ) -> RouteResult:
        """
        Route an order based on signal dict, Kyle lambda, and size.

        signal keys: symbol, side, price, horizon (int), confidence
        """
        symbol = signal.get("symbol", "BTC/USDT")
        side = signal.get("side", "buy")
        price = float(signal.get("price", 0.0))
        horizon_idx = int(signal.get("horizon", 5))

        algo = self._select_algo(horizon_idx, kyle_lambda, size_usd)

        try:
            best_venue = await self._best_venue(symbol, side)
            return await self._execute(best_venue, algo, signal, size_usd, price)
        except Exception as exc:
            log.warning("router_execution_failed", symbol=symbol, exc=str(exc))
            return RouteResult(
                venue="none",
                algo=algo,
                order_id=None,
                filled_qty=0.0,
                avg_price=0.0,
                fee_usd=0.0,
                slippage_bps=0.0,
                success=False,
                error=str(exc),
            )

    def _select_algo(self, horizon_idx: int, kyle_lambda: float, size_usd: float) -> str:
        """Select execution algorithm based on horizon index and market impact."""
        impact_est = kyle_lambda * size_usd
        if horizon_idx <= 1 and impact_est < 5:
            return "IOC"
        if horizon_idx <= 4 and impact_est < 20:
            return "iceberg"
        return "TWAP"

    async def _best_venue(self, symbol: str, side: str) -> str:
        """
        Compare top-of-book prices across all exchanges and return the best venue.

        For buy orders: venue with lowest ask.
        For sell orders: venue with highest bid.
        """
        prices: dict[str, float] = {}
        tasks = {
            name: asyncio.create_task(ex.fetch_order_book(symbol, limit=1))
            for name, ex in self._exchanges.items()
        }
        for name, task in tasks.items():
            try:
                ob = await asyncio.wait_for(task, timeout=3.0)
                if side == "buy" and ob.get("asks"):
                    prices[name] = ob["asks"][0][0]
                elif ob.get("bids"):
                    prices[name] = ob["bids"][0][0]
            except Exception as exc:
                log.debug("order_book_fetch_failed", venue=name, exc=str(exc))

        if not prices:
            return self._exchange_names[0]
        return min(prices, key=prices.get) if side == "buy" else max(prices, key=prices.get)  # type: ignore[arg-type]

    async def _execute(
        self,
        venue: str,
        algo: str,
        signal: dict,
        size_usd: float,
        price: float,
    ) -> RouteResult:
        """Execute order at the selected venue using the chosen algorithm."""
        ex = self._exchanges.get(venue)
        if ex is None:
            return RouteResult(
                venue=venue,
                algo=algo,
                order_id=None,
                filled_qty=0.0,
                avg_price=0.0,
                fee_usd=0.0,
                slippage_bps=0.0,
                success=False,
                error="exchange_not_available",
            )

        symbol = signal.get("symbol", "BTC/USDT")
        side = signal.get("side", "buy")
        qty = size_usd / max(price, 1e-9)

        if algo == "IOC":
            order = await ex.create_order(symbol, "limit", side, qty, price, {"timeInForce": "IOC"})
            return self._order_to_result(venue, algo, order, price, size_usd)

        if algo == "iceberg":
            return await self._iceberg(ex, venue, signal, qty, price)

        return await self._twap(ex, venue, signal, size_usd, price)

    async def _iceberg(
        self,
        ex: Any,
        venue: str,
        signal: dict,
        total_qty: float,
        price: float,
    ) -> RouteResult:
        """Execute as iceberg: 10 equal-sized slices placed sequentially."""
        symbol = signal.get("symbol", "BTC/USDT")
        side = signal.get("side", "buy")
        n_slices = 10
        slice_qty = total_qty / n_slices
        filled = 0.0
        total_cost = 0.0

        for i in range(n_slices):
            try:
                order = await ex.create_order(symbol, "limit", side, slice_qty, price)
                filled += float(order.get("filled", 0.0))
                total_cost += float(order.get("filled", 0.0)) * float(order.get("average", price))
                await asyncio.sleep(0.5)
            except Exception as exc:
                log.debug("iceberg_slice_failed", slice=i, exc=str(exc))

        avg_price = total_cost / max(filled, 1e-9)
        slippage_bps = abs(avg_price - price) / max(price, 1e-9) * 10_000
        return RouteResult(
            venue=venue,
            algo="iceberg",
            order_id=None,
            filled_qty=filled,
            avg_price=avg_price,
            fee_usd=total_cost * 0.0004,
            slippage_bps=slippage_bps,
            success=filled > 0,
        )

    async def _twap(
        self,
        ex: Any,
        venue: str,
        signal: dict,
        size_usd: float,
        price: float,
    ) -> RouteResult:
        """Execute as TWAP: 12 equal slices over horizon_seconds."""
        symbol = signal.get("symbol", "BTC/USDT")
        side = signal.get("side", "buy")
        horizon_seconds = int(signal.get("horizon_seconds", 300))
        n_slices = 12
        slice_usd = size_usd / n_slices
        interval = horizon_seconds / n_slices
        filled = 0.0
        total_cost = 0.0

        for i in range(n_slices):
            try:
                ob = await ex.fetch_order_book(symbol, limit=1)
                current_price = ob["asks"][0][0] if side == "buy" else ob["bids"][0][0]
                qty = slice_usd / max(current_price, 1e-9)
                order = await ex.create_order(symbol, "market", side, qty)
                filled += float(order.get("filled", 0.0))
                total_cost += float(order.get("filled", 0.0)) * float(
                    order.get("average", current_price)
                )
                if i < n_slices - 1:
                    await asyncio.sleep(interval)
            except Exception as exc:
                log.debug("twap_slice_failed", slice=i, exc=str(exc))

        avg_price = total_cost / max(filled, 1e-9)
        slippage_bps = abs(avg_price - price) / max(price, 1e-9) * 10_000
        return RouteResult(
            venue=venue,
            algo="TWAP",
            order_id=None,
            filled_qty=filled,
            avg_price=avg_price,
            fee_usd=total_cost * 0.0004,
            slippage_bps=slippage_bps,
            success=filled > 0,
        )

    def _order_to_result(
        self,
        venue: str,
        algo: str,
        order: dict,
        ref_price: float,
        size_usd: float,
    ) -> RouteResult:
        filled = float(order.get("filled", 0.0))
        avg_price = float(order.get("average", ref_price))
        slippage_bps = abs(avg_price - ref_price) / max(ref_price, 1e-9) * 10_000
        return RouteResult(
            venue=venue,
            algo=algo,
            order_id=str(order.get("id", "")),
            filled_qty=filled,
            avg_price=avg_price,
            fee_usd=float(order.get("fee", {}).get("cost", size_usd * 0.0004)),
            slippage_bps=slippage_bps,
            success=filled > 0,
        )

    async def close(self) -> None:
        """Close all exchange connections."""
        import contextlib

        for ex in self._exchanges.values():
            with contextlib.suppress(Exception):
                await ex.close()
