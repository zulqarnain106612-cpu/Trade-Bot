"""Fee extraction from a ccxt order in the smart router.

The router used to read the fee as
``float(order.get("fee", {}).get("cost", size_usd * 0.0004))``, which assumes
the ``fee`` key is either absent or a dict with a numeric ``cost``. ccxt
satisfies neither assumption reliably, and LiveExecutor._extract_fee_usd
already handled all of it -- the router just didn't.
"""

from __future__ import annotations

import pytest

from src.execution.router import _FEE_FALLBACK_RATE, _fee_usd_from_order


SIZE = 10_000.0
FALLBACK = SIZE * _FEE_FALLBACK_RATE


def test_a_null_fee_key_does_not_crash():
    """`.get("fee", {})` returns None, not {}, when the key exists and is null."""
    assert _fee_usd_from_order({"fee": None}, SIZE) == pytest.approx(FALLBACK)


def test_a_null_cost_does_not_crash():
    """`.get("cost", default)` returns None, not the default, for an explicit null."""
    assert _fee_usd_from_order({"fee": {"cost": None, "currency": "USDT"}}, SIZE) == pytest.approx(
        FALLBACK
    )


def test_the_modern_fees_list_is_read():
    """ccxt's current shape is a list; the old code ignored it entirely."""
    order = {
        "fees": [
            {"cost": 1.5, "currency": "USDT"},
            {"cost": 0.5, "currency": "USDC"},
        ]
    }
    assert _fee_usd_from_order(order, SIZE) == pytest.approx(2.0)


def test_the_legacy_single_fee_dict_is_still_read():
    assert _fee_usd_from_order({"fee": {"cost": 3.25, "currency": "USD"}}, SIZE) == pytest.approx(
        3.25
    )


def test_a_fee_billed_in_a_non_quote_currency_is_not_counted_as_dollars():
    """A 0.02 BNB fee is not $0.02."""
    order = {"fees": [{"cost": 0.02, "currency": "BNB"}]}
    assert _fee_usd_from_order(order, SIZE) == pytest.approx(FALLBACK)


def test_a_fee_with_no_currency_is_assumed_to_be_quote():
    assert _fee_usd_from_order({"fees": [{"cost": 4.0}]}, SIZE) == pytest.approx(4.0)


def test_an_unparseable_cost_falls_back_rather_than_raising():
    order = {"fees": [{"cost": "not-a-number", "currency": "USDT"}]}
    assert _fee_usd_from_order(order, SIZE) == pytest.approx(FALLBACK)


def test_a_malformed_fees_entry_is_skipped():
    order = {"fees": ["0.5", {"cost": 1.0, "currency": "USDT"}]}
    assert _fee_usd_from_order(order, SIZE) == pytest.approx(1.0)


def test_a_zero_fee_falls_back_to_the_notional_estimate():
    """A reported zero is indistinguishable from "not reported" here."""
    assert _fee_usd_from_order(
        {"fees": [{"cost": 0.0, "currency": "USDT"}]}, SIZE
    ) == pytest.approx(FALLBACK)


def test_an_order_with_no_fee_information_uses_the_estimate():
    assert _fee_usd_from_order({}, SIZE) == pytest.approx(FALLBACK)


def test_the_router_result_carries_the_parsed_fee():
    from src.execution.router import SmartOrderRouter

    router = SmartOrderRouter.__new__(SmartOrderRouter)
    result = router._order_to_result(
        venue="binance",
        algo="MARKET",
        order={"filled": 1.0, "average": 100.0, "id": "x", "fee": None},
        ref_price=100.0,
        size_usd=SIZE,
    )

    assert result.fee_usd == pytest.approx(FALLBACK)
    assert result.success
