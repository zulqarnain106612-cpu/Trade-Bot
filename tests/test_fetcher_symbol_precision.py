"""Market precision and limits, when ccxt reports them as null.

ccxt fills in the `precision` and `limits` keys for every market, including
ones whose exchange does not publish those numbers -- the values are then
None. `float(precision.get("amount", 8))` returns None from the .get and
raises, on the path that decides how an order is rounded and whether it
clears the venue minimum.
"""

from __future__ import annotations

import pytest

from src.data.fetcher import MarketDataFetcher, _as_float


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 8.0),
        (4, 4.0),
        ("2", 2.0),
        ("", 8.0),
        (object(), 8.0),
    ],
)
def test_as_float_falls_back_on_anything_unusable(value, expected):
    assert _as_float(value, 8.0) == pytest.approx(expected)


def _fetcher_with_market(market):
    fetcher = MarketDataFetcher.__new__(MarketDataFetcher)
    exchange = type("_Ex", (), {"markets": {"BTC/USDT": market}})()
    fetcher._require_binance = lambda: exchange
    return fetcher


@pytest.mark.asyncio
async def test_a_market_with_null_precision_uses_the_defaults():
    fetcher = _fetcher_with_market({"precision": {"amount": None, "price": None}, "limits": {}})

    meta = await fetcher.fetch_symbol_precision("BTC/USDT")

    assert meta["amount_precision"] == pytest.approx(8.0)
    assert meta["price_precision"] == pytest.approx(8.0)


@pytest.mark.asyncio
async def test_null_precision_and_limits_containers_are_tolerated():
    """Not just the values -- the containers themselves come back null."""
    fetcher = _fetcher_with_market({"precision": None, "limits": None})

    meta = await fetcher.fetch_symbol_precision("BTC/USDT")

    assert meta == {
        "amount_precision": pytest.approx(8.0),
        "price_precision": pytest.approx(8.0),
        "min_amount": pytest.approx(0.0),
        "min_cost": pytest.approx(0.0),
    }


@pytest.mark.asyncio
async def test_a_null_limit_container_is_tolerated():
    fetcher = _fetcher_with_market(
        {"precision": {"amount": 3, "price": 2}, "limits": {"amount": None, "cost": None}}
    )

    meta = await fetcher.fetch_symbol_precision("BTC/USDT")

    assert meta["amount_precision"] == pytest.approx(3.0)
    assert meta["min_amount"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_real_values_are_passed_through():
    fetcher = _fetcher_with_market(
        {
            "precision": {"amount": 3, "price": 2},
            "limits": {"amount": {"min": 0.0001}, "cost": {"min": 10.0}},
        }
    )

    meta = await fetcher.fetch_symbol_precision("BTC/USDT")

    assert meta == {
        "amount_precision": pytest.approx(3.0),
        "price_precision": pytest.approx(2.0),
        "min_amount": pytest.approx(0.0001),
        "min_cost": pytest.approx(10.0),
    }
