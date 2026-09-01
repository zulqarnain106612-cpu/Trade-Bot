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


# ---------------------------------------------------------------------------
# TWAP slice accounting and pacing
# ---------------------------------------------------------------------------


class _Exchange:
    """Minimal ccxt-shaped exchange whose slices can be told to fail."""

    def __init__(self, orders):
        self._orders = list(orders)
        self.submitted = 0

    async def fetch_order_book(self, _symbol, limit=1):
        return {"asks": [[100.0, 5.0]], "bids": [[99.0, 5.0]]}

    async def create_order(self, *_a, **_k):
        order = self._orders[self.submitted]
        self.submitted += 1
        if isinstance(order, Exception):
            raise order
        return order


async def _run_twap(monkeypatch, orders):
    import asyncio as _asyncio

    from src.execution.router import SmartOrderRouter

    sleeps: list[float] = []

    async def _record_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(_asyncio, "sleep", _record_sleep)

    router = SmartOrderRouter.__new__(SmartOrderRouter)
    ex = _Exchange(orders)

    async def _submit_slice(_ex, **kwargs):
        return await ex.create_order()

    router._submit_slice = _submit_slice
    result = await router._twap(
        ex,
        venue="binance",
        signal={"symbol": "BTC/USDT", "side": "buy", "horizon_seconds": 120},
        size_usd=SIZE,
        price=100.0,
        route_id="r1",
    )
    return result, sleeps


@pytest.mark.asyncio
async def test_a_failed_twap_slice_does_not_collapse_the_schedule(monkeypatch):
    """Skipping the wait turns the rest of the order into a burst."""
    orders = [RuntimeError("venue rejected")] + [
        {"filled": 1.0, "average": 100.0} for _ in range(11)
    ]

    _result, sleeps = await _run_twap(monkeypatch, orders)

    # 12 slices, so 11 waits — the failure must not cost one
    assert len(sleeps) == 11
    assert all(s == pytest.approx(10.0) for s in sleeps)


@pytest.mark.asyncio
async def test_a_slice_with_no_reported_average_is_still_priced(monkeypatch):
    """average=None used to raise mid-slice, crediting quantity but no cost."""
    orders = [{"filled": 1.0, "average": None} for _ in range(12)]

    result, _sleeps = await _run_twap(monkeypatch, orders)

    assert result.filled_qty == pytest.approx(12.0)
    # priced at the book instead of being dropped, so avg_price is the truth
    assert result.avg_price == pytest.approx(100.0)


async def _run_iceberg(monkeypatch, orders):
    import asyncio as _asyncio

    from src.execution.router import SmartOrderRouter

    sleeps: list[float] = []

    async def _record_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(_asyncio, "sleep", _record_sleep)

    router = SmartOrderRouter.__new__(SmartOrderRouter)
    ex = _Exchange(orders)

    async def _submit_slice(_ex, **kwargs):
        return await ex.create_order()

    router._submit_slice = _submit_slice
    result = await router._iceberg(
        ex,
        venue="binance",
        signal={"symbol": "BTC/USDT", "side": "buy"},
        total_qty=10.0,
        price=100.0,
        route_id="r2",
    )
    return result, sleeps


@pytest.mark.asyncio
async def test_a_failed_iceberg_slice_keeps_the_gap_before_the_next(monkeypatch):
    orders = [RuntimeError("rejected")] + [{"filled": 1.0, "average": 100.0} for _ in range(9)]

    _result, sleeps = await _run_iceberg(monkeypatch, orders)

    assert len(sleeps) == 10


@pytest.mark.asyncio
async def test_an_iceberg_slice_with_no_average_is_priced_at_the_limit(monkeypatch):
    orders = [{"filled": 1.0, "average": None} for _ in range(10)]

    result, _sleeps = await _run_iceberg(monkeypatch, orders)

    assert result.filled_qty == pytest.approx(10.0)
    assert result.avg_price == pytest.approx(100.0)
