"""
Tests for src/execution/live_fsm_integration.py.

The module is a thin wrapper, and the thing worth pinning is what it does
with failures: a timeout or an exchange error must reach the caller. An
order that silently returns instead of raising would be reported as placed
when it was not, which on this path means a position the book does not know
about.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import ccxt.async_support as ccxt
import pytest

from src.execution.live_fsm_integration import LiveExecutorOrderFSM


def _fsm_double(order_id: str = "abc-1") -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            order_id=order_id,
            status=SimpleNamespace(value="filled"),
            filled_qty=0.5,
            average_fill_price=50_000.123456,
        )
    )


def _wrapper(place_result=None, place_error=None) -> LiveExecutorOrderFSM:
    fetcher = MagicMock()
    fetcher.get_order_exchange = MagicMock(return_value=MagicMock())
    wrapper = LiveExecutorOrderFSM(fetcher)
    wrapper._order_manager = MagicMock()
    wrapper._order_manager.place_order_with_fsm = AsyncMock(
        return_value=place_result, side_effect=place_error
    )
    return wrapper


def test_init_builds_its_own_order_manager():
    fetcher = MagicMock()
    wrapper = LiveExecutorOrderFSM(fetcher)
    assert wrapper._fetcher is fetcher
    assert wrapper._order_manager is not None


async def test_returns_the_fsm_and_the_exchange_order():
    fsm = _fsm_double()
    order = {"id": "abc-1", "status": "closed"}
    wrapper = _wrapper(place_result=(fsm, order))

    got_fsm, got_order = await wrapper.place_market_order_with_fsm(
        symbol="BTC/USDT", side="buy", quantity=0.5
    )

    assert got_fsm is fsm
    assert got_order is order


async def test_places_against_the_order_exchange():
    fsm = _fsm_double()
    wrapper = _wrapper(place_result=(fsm, {}))

    await wrapper.place_market_order_with_fsm(symbol="ETH/USDT", side="sell", quantity=2.0)

    kwargs = wrapper._order_manager.place_order_with_fsm.await_args.kwargs
    assert kwargs["exchange"] is wrapper._fetcher.get_order_exchange.return_value
    assert kwargs["symbol"] == "ETH/USDT"
    assert kwargs["side"] == "sell"
    assert kwargs["quantity"] == 2.0


async def test_logs_an_unpriced_fill_without_raising():
    # average_fill_price is None until something fills; the log rounds it, so
    # a None here would be a TypeError on the success path.
    fsm = _fsm_double()
    fsm.state.average_fill_price = None
    wrapper = _wrapper(place_result=(fsm, {}))

    got_fsm, _ = await wrapper.place_market_order_with_fsm(
        symbol="BTC/USDT", side="buy", quantity=0.1
    )

    assert got_fsm is fsm


async def test_a_confirmation_timeout_reaches_the_caller():
    wrapper = _wrapper(place_error=TimeoutError("no confirmation"))

    with pytest.raises(TimeoutError):
        await wrapper.place_market_order_with_fsm(symbol="BTC/USDT", side="buy", quantity=0.5)


async def test_an_exchange_error_reaches_the_caller():
    wrapper = _wrapper(place_error=ccxt.ExchangeError("rejected"))

    with pytest.raises(ccxt.ExchangeError):
        await wrapper.place_market_order_with_fsm(symbol="BTC/USDT", side="buy", quantity=0.5)
