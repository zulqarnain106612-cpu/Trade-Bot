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

from src.execution.idempotency import (
    DuplicateOrderError,
    IdempotencyRegistry,
    client_order_id_params,
    derive_idempotency_key,
)

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

        # LAW3: one registry for the router, spanning every venue. Slices of a
        # sliced algo are separate submissions and each needs its own key, but
        # they must share a namespace so a re-routed order cannot replay a
        # slice that already went out on a different venue.
        self._idempotency = IdempotencyRegistry()

    @property
    def idempotency(self) -> IdempotencyRegistry:
        """Registry of idempotency keys seen by this router."""
        return self._idempotency

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

    @staticmethod
    def _route_id(signal: dict, size_usd: float) -> str:
        """
        Stable identity for one routing decision.

        A caller-supplied ``signal_id`` is preferred: it survives a re-route to
        a different venue or algorithm, which is exactly the case where the
        same intent could otherwise reach two exchanges. Without one, fall back
        to a time-bucketed hash of the signal's own content.
        """
        signal_id = signal.get("signal_id") or signal.get("id")
        if signal_id:
            return str(signal_id)
        return derive_idempotency_key(
            strategy_id="router",
            symbol=str(signal.get("symbol", "BTC/USDT")),
            side=str(signal.get("side", "buy")),
            quantity=size_usd,
            purpose="route",
        )

    async def _submit_slice(
        self,
        ex: Any,
        *,
        venue: str,
        route_id: str,
        slice_no: int,
        symbol: str,
        order_type: str,
        side: str,
        qty: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict:
        """
        Submit one slice with an idempotency key attached (LAW3).

        Each slice of a sliced algorithm is a distinct submission, so the key
        is scoped by ``(route_id, slice_no)``: replaying slice 3 is refused
        while slice 4 still goes out.
        """
        key = derive_idempotency_key(
            strategy_id="router",
            symbol=symbol,
            side=side,
            quantity=qty,
            purpose=f"{order_type}:{venue}",
            intent_id=f"{route_id}:{slice_no}",
        )
        try:
            await self._idempotency.reserve(key)
        except DuplicateOrderError as exc:
            # Logged here, at warning, because both slice loops swallow slice
            # exceptions into a debug line -- a suppressed duplicate would
            # otherwise be indistinguishable from an ordinary slice failure,
            # and the guard doing its job is exactly what an operator needs to
            # see after a reconnect or a re-route.
            log.warning(
                "router.duplicate_slice_suppressed",
                venue=venue,
                route_id=route_id,
                slice_no=slice_no,
                symbol=symbol,
                idempotency_key=key,
                prior_order_id=exc.record.order_id,
                prior_state=exc.record.state.value,
            )
            raise

        order_params = client_order_id_params(getattr(ex, "id", venue), key, params)
        try:
            order = await ex.create_order(symbol, order_type, side, qty, price, order_params)
        except Exception as exc:
            # The key stays claimed: ccxt raises the same exception type for
            # "rejected, nothing placed" and "sent, response lost", and this
            # layer cannot tell them apart. Refusing the retry is the safe
            # direction -- an unfilled slice costs opportunity, a duplicated
            # one costs real money.
            await self._idempotency.fail(key, str(exc), retryable=False)
            raise
        await self._idempotency.complete(key, str(order.get("id") or ""), order)
        return order

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

        route_id = self._route_id(signal, size_usd)

        if algo == "IOC":
            order = await self._submit_slice(
                ex,
                venue=venue,
                route_id=route_id,
                slice_no=0,
                symbol=symbol,
                order_type="limit",
                side=side,
                qty=qty,
                price=price,
                params={"timeInForce": "IOC"},
            )
            return self._order_to_result(venue, algo, order, price, size_usd)

        if algo == "iceberg":
            return await self._iceberg(ex, venue, signal, qty, price, route_id)

        return await self._twap(ex, venue, signal, size_usd, price, route_id)

    async def _iceberg(
        self,
        ex: Any,
        venue: str,
        signal: dict,
        total_qty: float,
        price: float,
        route_id: str,
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
                order = await self._submit_slice(
                    ex,
                    venue=venue,
                    route_id=route_id,
                    slice_no=i,
                    symbol=symbol,
                    order_type="limit",
                    side=side,
                    qty=slice_qty,
                    price=price,
                )
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
        route_id: str,
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
                order = await self._submit_slice(
                    ex,
                    venue=venue,
                    route_id=route_id,
                    slice_no=i,
                    symbol=symbol,
                    order_type="market",
                    side=side,
                    qty=qty,
                )
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
