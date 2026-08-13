"""
LAW3 — idempotency keys and duplicate-submission rejection.

Covers the three ways a duplicate reaches the exchange in production: a retry
after an error, a WebSocket reconnect replaying an intent, and a reconciliation
pass re-issuing an order it could not match. All three reduce to "the same
intent submitted twice", which is what these tests assert is refused.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import ccxt.async_support as ccxt
import pytest

from src.execution.idempotency import (
    KEY_MAX_LEN,
    DuplicateOrderError,
    IdempotencyRegistry,
    SubmissionState,
    client_order_id_params,
    derive_idempotency_key,
)
from src.execution.order_fsm import OrderFSMError
from src.execution.order_manager import OrderManager
from src.execution.router import SmartOrderRouter


def _intent(**over) -> dict:
    base = {
        "strategy_id": "signal_engine_v1",
        "symbol": "BTC/USDT",
        "side": "buy",
        "quantity": 1.5,
        "purpose": "entry",
    }
    base.update(over)
    return base


def _exchange(order_id: str = "ord-1", venue: str = "binance") -> MagicMock:
    ex = MagicMock()
    ex.id = venue
    ex.create_market_order = AsyncMock(
        return_value={"id": order_id, "status": "closed", "filled": 1.5, "average": 65000.0}
    )
    ex.fetch_order = AsyncMock(
        return_value={"id": order_id, "status": "closed", "filled": 1.5, "average": 65000.0}
    )
    return ex


class TestKeyDerivation:
    def test_same_intent_yields_same_key(self):
        assert derive_idempotency_key(**_intent(), intent_id="trade-1") == derive_idempotency_key(
            **_intent(), intent_id="trade-1"
        )

    def test_differing_field_yields_different_key(self):
        base = derive_idempotency_key(**_intent(), intent_id="t1")
        for field, value in [
            ("side", "sell"),
            ("symbol", "ETH/USDT"),
            ("quantity", 1.6),
            ("purpose", "close"),
            ("strategy_id", "other"),
        ]:
            assert derive_idempotency_key(**_intent(**{field: value}), intent_id="t1") != base

    def test_entry_and_flatten_of_same_size_do_not_collide(self):
        """The emergency flatten shares symbol and quantity with the entry it
        undoes; only `purpose` and `intent_id` separate them."""
        entry = derive_idempotency_key(**_intent(purpose="entry"))
        flatten = derive_idempotency_key(
            **_intent(side="sell", purpose="emergency_flatten"), intent_id="ord-1"
        )
        assert entry != flatten

    def test_quantity_is_quantised_below_the_settled_unit(self):
        """Sizing recomputed on a retry will not reproduce bit-for-bit; a key
        that moves with the 12th decimal is not an idempotency key."""
        a = derive_idempotency_key(**_intent(quantity=1.5000000001), intent_id="t1")
        b = derive_idempotency_key(**_intent(quantity=1.5), intent_id="t1")
        assert a == b

    def test_intent_id_makes_key_time_independent(self):
        """A pinned intent (closing trade X) must survive a retry that crosses
        a bucket boundary, or the retry submits a second exit order."""
        a = derive_idempotency_key(**_intent(), intent_id="t1", now=0.0)
        b = derive_idempotency_key(**_intent(), intent_id="t1", now=10_000.0)
        assert a == b

    def test_without_intent_id_key_is_time_bucketed(self):
        same_bucket = derive_idempotency_key(**_intent(), now=10.0) == derive_idempotency_key(
            **_intent(), now=59.0
        )
        next_bucket = derive_idempotency_key(**_intent(), now=10.0) == derive_idempotency_key(
            **_intent(), now=61.0
        )
        assert same_bucket
        assert not next_bucket

    def test_key_fits_the_strictest_venue_limit(self):
        """OKX caps clOrdId at 32 alphanumeric characters -- the tightest of
        the supported venues, so one form is valid everywhere."""
        key = derive_idempotency_key(**_intent())
        assert len(key) == KEY_MAX_LEN == 32
        assert key.isalnum()

    def test_non_positive_bucket_rejected(self):
        with pytest.raises(ValueError, match="bucket_s must be positive"):
            derive_idempotency_key(**_intent(), bucket_s=0)


class TestClientOrderIdParams:
    @pytest.mark.parametrize(
        ("venue", "expected_field"),
        [
            ("binance", "newClientOrderId"),
            ("binanceusdm", "newClientOrderId"),
            ("okx", "clOrdId"),
            ("bybit", "orderLinkId"),
            ("kraken", "clientOrderId"),
            (None, "clientOrderId"),
        ],
    )
    def test_venue_specific_field_name(self, venue, expected_field):
        params = client_order_id_params(venue, "tbabc")
        assert params[expected_field] == "tbabc"

    def test_existing_params_are_preserved(self):
        params = client_order_id_params("binance", "tbabc", {"timeInForce": "IOC"})
        assert params["timeInForce"] == "IOC"
        assert params["newClientOrderId"] == "tbabc"

    def test_caller_supplied_id_is_not_overwritten(self):
        params = client_order_id_params("binance", "tbabc", {"newClientOrderId": "broker-tag-1"})
        assert params["newClientOrderId"] == "broker-tag-1"

    def test_caller_params_are_not_mutated(self):
        original: dict = {"timeInForce": "IOC"}
        client_order_id_params("binance", "tbabc", original)
        assert original == {"timeInForce": "IOC"}


class TestRegistry:
    @pytest.mark.asyncio
    async def test_reserve_twice_raises_duplicate(self):
        reg = IdempotencyRegistry()
        await reg.reserve("k1")
        with pytest.raises(DuplicateOrderError) as exc:
            await reg.reserve("k1")
        assert exc.value.key == "k1"
        assert exc.value.record.state is SubmissionState.IN_FLIGHT

    @pytest.mark.asyncio
    async def test_completed_key_cannot_be_reused(self):
        reg = IdempotencyRegistry()
        await reg.reserve("k1")
        await reg.complete("k1", "ord-1")
        with pytest.raises(DuplicateOrderError) as exc:
            await reg.reserve("k1")
        assert exc.value.record.order_id == "ord-1"

    @pytest.mark.asyncio
    async def test_retryable_failure_releases_the_key(self):
        """An exchange that answered and refused placed nothing, so the intent
        may legitimately be retried."""
        reg = IdempotencyRegistry()
        await reg.reserve("k1")
        await reg.fail("k1", "insufficient funds", retryable=True)
        assert not await reg.seen("k1")
        await reg.reserve("k1")  # must not raise

    @pytest.mark.asyncio
    async def test_non_retryable_failure_keeps_the_key_claimed(self):
        """A network error may mean 'executed, response lost'. Resubmitting is
        the one outcome worse than not filling."""
        reg = IdempotencyRegistry()
        await reg.reserve("k1")
        await reg.fail("k1", "connection reset", retryable=False)
        assert await reg.seen("k1")
        with pytest.raises(DuplicateOrderError):
            await reg.reserve("k1")

    @pytest.mark.asyncio
    async def test_expired_key_is_evicted_and_reusable(self):
        reg = IdempotencyRegistry(ttl_s=0.01)
        await reg.reserve("k1")
        await reg.complete("k1", "ord-1")
        await asyncio.sleep(0.05)
        assert await reg.get("k1") is None
        await reg.reserve("k1")  # must not raise

    @pytest.mark.asyncio
    async def test_capacity_eviction_spares_in_flight_keys(self):
        reg = IdempotencyRegistry(max_entries=2)
        await reg.reserve("inflight")
        for i in range(5):
            await reg.reserve(f"done-{i}")
            await reg.complete(f"done-{i}", f"ord-{i}")
        assert await reg.seen("inflight")

    @pytest.mark.asyncio
    async def test_concurrent_reserves_admit_exactly_one(self):
        """Two coroutines racing on the same intent is the reconnect case."""
        reg = IdempotencyRegistry()
        results = await asyncio.gather(
            *(reg.reserve("k1") for _ in range(10)), return_exceptions=True
        )
        admitted = [r for r in results if not isinstance(r, Exception)]
        assert len(admitted) == 1
        assert all(isinstance(r, DuplicateOrderError) for r in results if isinstance(r, Exception))

    @pytest.mark.asyncio
    async def test_invalid_ttl_rejected(self):
        with pytest.raises(ValueError, match="ttl_s must be positive"):
            IdempotencyRegistry(ttl_s=0)


class TestOrderManagerDeduplication:
    @pytest.mark.asyncio
    async def test_missing_key_is_rejected(self):
        with pytest.raises(OrderFSMError, match="idempotency_key is required"):
            await OrderManager().place_order_with_fsm(_exchange(), "BTC/USDT", "buy", 1.5, "")

    @pytest.mark.asyncio
    async def test_duplicate_submission_never_reaches_the_exchange(self):
        mgr = OrderManager()
        ex = _exchange()
        key = derive_idempotency_key(**_intent(), intent_id="t1")

        await mgr.place_order_with_fsm(ex, "BTC/USDT", "buy", 1.5, key)
        with pytest.raises(DuplicateOrderError):
            await mgr.place_order_with_fsm(ex, "BTC/USDT", "buy", 1.5, key)

        assert ex.create_market_order.await_count == 1

    @pytest.mark.asyncio
    async def test_client_order_id_is_sent_to_the_exchange(self):
        """The only defence that survives a crash between submit and ack."""
        mgr = OrderManager()
        ex = _exchange(venue="okx")
        await mgr.place_order_with_fsm(ex, "BTC/USDT", "buy", 1.5, "tbdeadbeef")
        assert ex.create_market_order.await_args.kwargs["params"]["clOrdId"] == "tbdeadbeef"

    @pytest.mark.asyncio
    async def test_key_recorded_on_fsm_state(self):
        mgr = OrderManager()
        fsm, _ = await mgr.place_order_with_fsm(_exchange(), "BTC/USDT", "buy", 1.5, "tbkey1")
        assert fsm.state.idempotency_key == "tbkey1"
        assert fsm.state.to_dict()["idempotency_key"] == "tbkey1"

    @pytest.mark.asyncio
    async def test_exchange_rejection_releases_key_for_retry(self):
        mgr = OrderManager()
        ex = _exchange()
        ex.create_market_order = AsyncMock(side_effect=ccxt.InvalidOrder("bad lot size"))
        with pytest.raises(ccxt.InvalidOrder):
            await mgr.place_order_with_fsm(ex, "BTC/USDT", "buy", 1.5, "tbkey2")
        assert not await mgr.idempotency.seen("tbkey2")

    @pytest.mark.asyncio
    async def test_network_error_keeps_key_claimed(self):
        """The request may have been executed with the response lost -- a
        retry must be refused and the order left to reconciliation."""
        mgr = OrderManager()
        ex = _exchange()
        ex.create_market_order = AsyncMock(side_effect=ccxt.NetworkError("reset"))
        with pytest.raises(ccxt.NetworkError):
            await mgr.place_order_with_fsm(ex, "BTC/USDT", "buy", 1.5, "tbkey3")

        ex.create_market_order = AsyncMock(
            return_value={"id": "ord-9", "status": "closed", "filled": 1.5, "average": 1.0}
        )
        with pytest.raises(DuplicateOrderError):
            await mgr.place_order_with_fsm(ex, "BTC/USDT", "buy", 1.5, "tbkey3")
        assert ex.create_market_order.await_count == 0

    @pytest.mark.asyncio
    async def test_distinct_intents_both_go_through(self):
        mgr = OrderManager()
        ex = _exchange()
        await mgr.place_order_with_fsm(
            ex, "BTC/USDT", "buy", 1.5, derive_idempotency_key(**_intent(), intent_id="t1")
        )
        await mgr.place_order_with_fsm(
            ex, "BTC/USDT", "buy", 1.5, derive_idempotency_key(**_intent(), intent_id="t2")
        )
        assert ex.create_market_order.await_count == 2

    @pytest.mark.asyncio
    async def test_injected_registry_survives_manager_replacement(self):
        """A registry rebuilt on reconnect cannot detect the reconnect replay
        it exists to stop, so it must be injectable."""
        shared = IdempotencyRegistry()
        ex = _exchange()

        await OrderManager(shared).place_order_with_fsm(ex, "BTC/USDT", "buy", 1.5, "tbkey4")
        with pytest.raises(DuplicateOrderError):
            await OrderManager(shared).place_order_with_fsm(ex, "BTC/USDT", "buy", 1.5, "tbkey4")
        assert ex.create_market_order.await_count == 1


class TestRouterDeduplication:
    @staticmethod
    def _router(venue: str = "binance") -> tuple[SmartOrderRouter, MagicMock]:
        router = SmartOrderRouter(exchanges=[])
        ex = MagicMock()
        ex.id = venue
        ex.create_order = AsyncMock(
            return_value={"id": "ord-1", "filled": 1.0, "average": 100.0, "fee": None}
        )
        router._exchanges[venue] = ex
        return router, ex

    @pytest.mark.asyncio
    async def test_slice_carries_client_order_id(self):
        router, ex = self._router("okx")
        await router._submit_slice(
            ex,
            venue="okx",
            route_id="r1",
            slice_no=0,
            symbol="BTC/USDT",
            order_type="market",
            side="buy",
            qty=1.0,
        )
        assert ex.create_order.await_args.args[5]["clOrdId"].startswith("tb")

    @pytest.mark.asyncio
    async def test_replayed_slice_is_refused(self):
        router, ex = self._router()
        kwargs = {
            "venue": "binance",
            "route_id": "r1",
            "slice_no": 3,
            "symbol": "BTC/USDT",
            "order_type": "limit",
            "side": "buy",
            "qty": 1.0,
            "price": 100.0,
        }
        await router._submit_slice(ex, **kwargs)
        with pytest.raises(DuplicateOrderError):
            await router._submit_slice(ex, **kwargs)
        assert ex.create_order.await_count == 1

    @pytest.mark.asyncio
    async def test_sibling_slices_are_independent(self):
        """Refusing a replay of slice 3 must not block slice 4."""
        router, ex = self._router()
        for slice_no in (3, 4):
            await router._submit_slice(
                ex,
                venue="binance",
                route_id="r1",
                slice_no=slice_no,
                symbol="BTC/USDT",
                order_type="limit",
                side="buy",
                qty=1.0,
                price=100.0,
            )
        assert ex.create_order.await_count == 2

    @pytest.mark.asyncio
    async def test_reroute_to_another_venue_cannot_replay_a_slice(self):
        """The registry spans venues, so a re-route cannot double-submit."""
        router, ex = self._router()
        other = MagicMock()
        other.id = "okx"
        other.create_order = AsyncMock(return_value={"id": "ord-2", "filled": 1.0})
        router._exchanges["okx"] = other

        key = derive_idempotency_key(
            strategy_id="router",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            purpose="market:binance",
            intent_id="r1:0",
        )
        await router.idempotency.reserve(key)
        with pytest.raises(DuplicateOrderError):
            await router._submit_slice(
                ex,
                venue="binance",
                route_id="r1",
                slice_no=0,
                symbol="BTC/USDT",
                order_type="market",
                side="buy",
                qty=1.0,
            )
        assert ex.create_order.await_count == 0

    @pytest.mark.asyncio
    async def test_failed_slice_keeps_key_claimed(self):
        """ccxt raises the same type for 'rejected' and 'sent, reply lost';
        the router cannot tell them apart, so it refuses the retry."""
        router, ex = self._router()
        ex.create_order = AsyncMock(side_effect=ccxt.NetworkError("reset"))
        kwargs = {
            "venue": "binance",
            "route_id": "r1",
            "slice_no": 0,
            "symbol": "BTC/USDT",
            "order_type": "market",
            "side": "buy",
            "qty": 1.0,
        }
        with pytest.raises(ccxt.NetworkError):
            await router._submit_slice(ex, **kwargs)
        with pytest.raises(DuplicateOrderError):
            await router._submit_slice(ex, **kwargs)

    @pytest.mark.asyncio
    async def test_caller_signal_id_pins_the_route(self):
        router, _ = self._router()
        assert router._route_id({"signal_id": "sig-7"}, 100.0) == "sig-7"

    @pytest.mark.asyncio
    async def test_route_id_falls_back_to_signal_content(self):
        # Time is frozen: the fallback is time-bucketed, so two real calls
        # either side of a bucket boundary would differ and make this flaky.
        router, _ = self._router()
        signal = {"symbol": "BTC/USDT", "side": "buy"}
        with patch("src.execution.idempotency.time.time", return_value=1_000.0):
            assert router._route_id(signal, 100.0) == router._route_id(dict(signal), 100.0)
            assert router._route_id(signal, 100.0) != router._route_id(signal, 200.0)
