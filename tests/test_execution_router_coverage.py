"""Tests for src/execution/router.py -- Kyle-lambda multi-venue SmartOrderRouter.

Every exchange is a fake injected into _exchanges after construction, so no
test opens a ccxt connection or reaches a live venue. asyncio.sleep is
patched out in the sliced-algo tests so TWAP's 12 slices don't actually
wait out their interval.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.execution.idempotency import DuplicateOrderError
from src.execution.router import RouteResult, SmartOrderRouter


def _router(venues: list[str] | None = None) -> SmartOrderRouter:
    """Build a router with no real ccxt exchange instances attached."""
    with (
        patch("ccxt.async_support.binance"),
        patch("ccxt.async_support.bybit"),
        patch("ccxt.async_support.okx"),
    ):
        router = SmartOrderRouter(exchanges=venues if venues is not None else [])
    return router


def _fake_exchange(order: dict | None = None, book: dict | None = None) -> MagicMock:
    ex = MagicMock()
    ex.id = "fake"
    ex.create_order = AsyncMock(return_value=order or {"id": "o1", "filled": 1.0, "average": 100.0})
    ex.fetch_order_book = AsyncMock(
        return_value=book or {"asks": [[100.0, 5.0]], "bids": [[99.0, 5.0]]}
    )
    ex.close = AsyncMock()
    return ex


def test_init_empty_list_connects_to_no_venues():
    router = _router([])
    assert router._exchange_names == []
    assert router._exchanges == {}


def test_init_none_uses_three_default_venues():
    with (
        patch("ccxt.async_support.binance"),
        patch("ccxt.async_support.bybit"),
        patch("ccxt.async_support.okx"),
    ):
        router = SmartOrderRouter(exchanges=None)
    assert router._exchange_names == ["binance", "bybit", "okx"]


def test_init_unknown_venue_is_skipped():
    router = _router(["definitely_not_an_exchange"])
    assert router._exchanges == {}


def test_idempotency_property_exposes_registry():
    router = _router([])
    assert router.idempotency is router._idempotency


def test_select_algo_ioc_for_fast_horizon_and_low_impact():
    router = _router([])
    assert router._select_algo(horizon_idx=0, kyle_lambda=0.0001, size_usd=1000) == "IOC"


def test_select_algo_iceberg_for_mid_horizon():
    router = _router([])
    assert router._select_algo(horizon_idx=3, kyle_lambda=0.001, size_usd=1000) == "iceberg"


def test_select_algo_twap_for_slow_horizon():
    router = _router([])
    assert router._select_algo(horizon_idx=9, kyle_lambda=0.001, size_usd=1000) == "TWAP"


def test_select_algo_twap_when_impact_too_high_despite_fast_horizon():
    router = _router([])
    assert router._select_algo(horizon_idx=0, kyle_lambda=1.0, size_usd=1000) == "TWAP"


def test_route_id_prefers_explicit_signal_id():
    router = _router([])
    assert router._route_id({"signal_id": "sig-123"}, 1000.0) == "sig-123"


def test_route_id_falls_back_to_id_key():
    router = _router([])
    assert router._route_id({"id": "alt-456"}, 1000.0) == "alt-456"


def test_route_id_derives_key_when_no_id_present():
    router = _router([])
    key = router._route_id({"symbol": "BTC/USDT", "side": "buy"}, 1000.0)
    assert isinstance(key, str) and key


async def test_best_venue_no_exchanges_returns_first_name():
    router = _router(["binance"])
    router._exchanges = {}
    assert await router._best_venue("BTC/USDT", "buy") == "binance"


async def test_best_venue_buy_picks_lowest_ask():
    router = _router([])
    router._exchanges = {
        "cheap": _fake_exchange(book={"asks": [[100.0, 1]], "bids": [[99.0, 1]]}),
        "dear": _fake_exchange(book={"asks": [[105.0, 1]], "bids": [[104.0, 1]]}),
    }
    assert await router._best_venue("BTC/USDT", "buy") == "cheap"


async def test_best_venue_sell_picks_highest_bid():
    router = _router([])
    router._exchanges = {
        "low": _fake_exchange(book={"asks": [[100.0, 1]], "bids": [[99.0, 1]]}),
        "high": _fake_exchange(book={"asks": [[106.0, 1]], "bids": [[105.0, 1]]}),
    }
    assert await router._best_venue("BTC/USDT", "sell") == "high"


async def test_best_venue_skips_venue_whose_book_fetch_fails():
    router = _router(["broken"])
    broken = _fake_exchange()
    broken.fetch_order_book = AsyncMock(side_effect=RuntimeError("timeout"))
    router._exchanges = {"broken": broken}
    assert await router._best_venue("BTC/USDT", "buy") == "broken"  # falls back to first name


async def test_execute_unknown_venue_returns_unsuccessful_result():
    router = _router([])
    result = await router._execute("nope", "IOC", {"symbol": "BTC/USDT"}, 1000.0, 100.0)
    assert result.success is False
    assert result.error == "exchange_not_available"


async def test_execute_ioc_happy_path():
    router = _router([])
    ex = _fake_exchange({"id": "o1", "filled": 10.0, "average": 100.5})
    router._exchanges = {"binance": ex}
    result = await router._execute(
        "binance", "IOC", {"symbol": "BTC/USDT", "side": "buy", "signal_id": "s1"}, 1000.0, 100.0
    )
    assert result.success is True
    assert result.algo == "IOC"
    assert result.filled_qty == 10.0
    ioc_params = ex.create_order.call_args[0][5]
    assert ioc_params["timeInForce"] == "IOC"


async def test_route_catches_execution_failure():
    router = _router([])
    ex = _fake_exchange()
    ex.create_order = AsyncMock(side_effect=RuntimeError("venue down"))
    router._exchanges = {"binance": ex}
    result = await router.route(
        {"symbol": "BTC/USDT", "side": "buy", "price": 100.0, "horizon": 0, "signal_id": "s1"},
        kyle_lambda=0.0001,
        size_usd=100.0,
    )
    assert result.success is False
    assert result.venue == "none"
    assert "venue down" in result.error


async def test_route_end_to_end_ioc():
    router = _router([])
    ex = _fake_exchange({"id": "o9", "filled": 1.0, "average": 100.0})
    router._exchanges = {"binance": ex}
    result = await router.route(
        {"symbol": "BTC/USDT", "side": "buy", "price": 100.0, "horizon": 0, "signal_id": "s2"},
        kyle_lambda=0.0001,
        size_usd=100.0,
    )
    assert result.success is True
    assert result.algo == "IOC"


async def test_submit_slice_duplicate_key_raises_and_is_logged():
    router = _router([])
    ex = _fake_exchange()
    await router._submit_slice(
        ex,
        venue="binance",
        route_id="r1",
        slice_no=0,
        symbol="BTC/USDT",
        order_type="limit",
        side="buy",
        qty=1.0,
        price=100.0,
    )
    with pytest.raises(DuplicateOrderError):
        await router._submit_slice(
            ex,
            venue="binance",
            route_id="r1",
            slice_no=0,
            symbol="BTC/USDT",
            order_type="limit",
            side="buy",
            qty=1.0,
            price=100.0,
        )


async def test_submit_slice_marks_key_failed_when_create_order_raises():
    router = _router([])
    ex = _fake_exchange()
    ex.create_order = AsyncMock(side_effect=RuntimeError("rejected"))
    with pytest.raises(RuntimeError):
        await router._submit_slice(
            ex,
            venue="binance",
            route_id="r2",
            slice_no=0,
            symbol="BTC/USDT",
            order_type="limit",
            side="buy",
            qty=1.0,
            price=100.0,
        )


async def test_iceberg_fills_across_slices():
    router = _router([])
    ex = _fake_exchange({"id": "o1", "filled": 0.1, "average": 100.0})
    with patch("src.execution.router.asyncio.sleep", new=AsyncMock()):
        result = await router._iceberg(
            ex,
            "binance",
            {"symbol": "BTC/USDT", "side": "buy"},
            total_qty=1.0,
            price=100.0,
            route_id="r3",
        )
    assert result.algo == "iceberg"
    assert result.success is True
    assert ex.create_order.await_count == 10


async def test_iceberg_survives_every_slice_failing():
    router = _router([])
    ex = _fake_exchange()
    ex.create_order = AsyncMock(side_effect=RuntimeError("no fill"))
    with patch("src.execution.router.asyncio.sleep", new=AsyncMock()):
        result = await router._iceberg(
            ex,
            "binance",
            {"symbol": "BTC/USDT", "side": "buy"},
            total_qty=1.0,
            price=100.0,
            route_id="r4",
        )
    assert result.success is False
    assert result.filled_qty == 0.0


async def test_twap_fills_across_slices():
    router = _router([])
    ex = _fake_exchange({"id": "o1", "filled": 0.5, "average": 100.0})
    with patch("src.execution.router.asyncio.sleep", new=AsyncMock()):
        result = await router._twap(
            ex,
            "binance",
            {"symbol": "BTC/USDT", "side": "buy"},
            size_usd=1200.0,
            price=100.0,
            route_id="r5",
        )
    assert result.algo == "TWAP"
    assert result.success is True
    assert ex.create_order.await_count == 12


async def test_twap_sell_side_uses_bid_price():
    router = _router([])
    ex = _fake_exchange({"id": "o1", "filled": 0.5, "average": 99.0})
    with patch("src.execution.router.asyncio.sleep", new=AsyncMock()):
        result = await router._twap(
            ex,
            "binance",
            {"symbol": "BTC/USDT", "side": "sell"},
            size_usd=1200.0,
            price=99.0,
            route_id="r6",
        )
    assert result.success is True


async def test_twap_survives_slice_failures():
    router = _router([])
    ex = _fake_exchange()
    ex.fetch_order_book = AsyncMock(side_effect=RuntimeError("book down"))
    with patch("src.execution.router.asyncio.sleep", new=AsyncMock()):
        result = await router._twap(
            ex,
            "binance",
            {"symbol": "BTC/USDT", "side": "buy"},
            size_usd=1200.0,
            price=100.0,
            route_id="r7",
        )
    assert result.success is False


def test_order_to_result_uses_fee_from_order_when_present():
    router = _router([])
    result = router._order_to_result(
        "binance",
        "IOC",
        {"id": "o1", "filled": 2.0, "average": 101.0, "fee": {"cost": 0.42}},
        100.0,
        1000.0,
    )
    assert result.fee_usd == 0.42
    assert result.slippage_bps == pytest.approx(100.0)
    assert result.success is True


def test_order_to_result_falls_back_to_notional_fee_estimate():
    router = _router([])
    result = router._order_to_result("binance", "IOC", {"id": "o1", "filled": 1.0}, 100.0, 1000.0)
    assert result.fee_usd == pytest.approx(1000.0 * 0.0004)


def test_order_to_result_unfilled_order_is_unsuccessful():
    router = _router([])
    result = router._order_to_result("binance", "IOC", {"id": "o1", "filled": 0.0}, 100.0, 1000.0)
    assert result.success is False


async def test_close_suppresses_exchange_errors():
    router = _router([])
    good = _fake_exchange()
    bad = _fake_exchange()
    bad.close = AsyncMock(side_effect=RuntimeError("already closed"))
    router._exchanges = {"good": good, "bad": bad}
    await router.close()  # must not raise
    good.close.assert_awaited_once()


def test_route_result_dataclass_defaults():
    r = RouteResult(
        venue="v",
        algo="IOC",
        order_id=None,
        filled_qty=0.0,
        avg_price=0.0,
        fee_usd=0.0,
        slippage_bps=0.0,
        success=False,
    )
    assert r.error is None
