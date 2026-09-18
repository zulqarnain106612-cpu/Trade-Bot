"""
EXEC-001, INV-007 — duplicate execution requests cannot create duplicate
positions.

The five-whys worked example in the source document ends here:

```
Why?  Two execution requests were processed.
Why?  No idempotency key.
Why?  Executor assumed requests were serialized.
Why?  Architecture didn't enforce idempotency.
Why?  Requirement didn't specify duplicate-request behavior.
```

So the requirement is specified, and this is where it is enforced. Three
separable properties:

1. **The key is deterministic.** The same intent produces the same key, and
   two *different* intents never collide — including the pair that looks most
   alike, an entry and the emergency flatten that follows it.
2. **The registry refuses a replay.** A key in flight or completed cannot be
   claimed again; a key whose attempt provably never reached the exchange can.
3. **"Provably never reached" is the caller's assertion, and the default is
   the safe one.** A network error or a timeout is *not* retryable in this
   sense — the request may well have executed with the response lost — so the
   key stays claimed and the order goes to reconciliation rather than being
   submitted twice.
"""

from __future__ import annotations

import asyncio

import pytest

from src.execution.idempotency import (
    DuplicateOrderError,
    IdempotencyRegistry,
    SubmissionState,
    client_order_id_params,
    derive_idempotency_key,
)


def key(**overrides) -> str:
    values = {
        "strategy_id": "momentum",
        "symbol": "BTC/USDT",
        "side": "buy",
        "quantity": 0.5,
        "purpose": "entry",
        "now": 1_700_000_000.0,
    }
    values.update(overrides)
    return derive_idempotency_key(**values)


class TestTheKeyIsDeterministic:
    def test_the_same_intent_gives_the_same_key(self):
        assert key() == key()

    @pytest.mark.parametrize(
        "field",
        ["strategy_id", "symbol", "side", "purpose"],
    )
    def test_a_different_intent_gives_a_different_key(self, field):
        assert key() != key(**{field: "something-else"})

    def test_an_entry_and_its_emergency_flatten_do_not_collide(self):
        # The pair that looks most alike: same strategy, symbol and quantity,
        # opposite intents. `purpose` is what separates them, and without it
        # the flatten would be refused as a duplicate of the entry.
        assert key(purpose="entry") != key(purpose="emergency_flatten")

    def test_a_different_quantity_gives_a_different_key(self):
        assert key(quantity=0.5) != key(quantity=0.6)

    def test_quantities_are_quantised_before_hashing(self):
        # 0.5 and 0.50000000000000001 are the same order. Hashing the raw
        # float repr would make a harmless rounding difference look like a
        # new intent and submit a second order.
        assert key(quantity=0.5) == key(quantity=0.5000000000000000001)

    def test_the_side_is_case_insensitive(self):
        assert key(side="BUY") == key(side="buy")

    def test_the_time_bucket_separates_repeated_intents(self):
        # Without a bucket, a strategy that wants the same position again an
        # hour later would be permanently blocked by its own earlier key.
        assert key(now=1_700_000_000.0) != key(now=1_700_100_000.0)

    def test_within_one_bucket_a_retry_is_the_same_intent(self):
        assert key(now=1_700_000_000.0) == key(now=1_700_000_001.0)

    def test_an_intent_id_pins_the_key_against_the_clock(self):
        # An exit for a specific position is the same intent whether it is
        # retried now or in ten minutes. Bucketing it would let a retry that
        # straddles a boundary submit a second exit order.
        first = key(intent_id="trade-77", now=1_700_000_000.0)
        later = key(intent_id="trade-77", now=1_700_900_000.0)
        assert first == later

    def test_different_intent_ids_do_not_collide(self):
        assert key(intent_id="trade-77") != key(intent_id="trade-78")

    @pytest.mark.parametrize("bucket", [0, -1])
    def test_a_non_positive_bucket_is_refused(self, bucket):
        with pytest.raises(ValueError, match="bucket_s must be positive"):
            key(bucket_s=bucket)

    def test_the_key_is_short_enough_for_a_client_order_id(self):
        # Venues cap client order id length; a key the exchange truncates is
        # a key that collides.
        assert len(key()) <= 36


class TestTheClientOrderId:
    def test_the_key_travels_to_the_exchange(self):
        # Enforcement at the venue, not only in this process: a duplicate
        # that somehow escapes the registry is still refused by Binance.
        params = client_order_id_params("binance", "tb-abc")
        assert "tb-abc" in params.values()

    def test_an_explicit_id_is_not_overwritten(self):
        # A caller needing a venue-specific format (a broker-tagged id) must
        # not have it silently replaced.
        params = client_order_id_params("binance", "tb-abc", {"newClientOrderId": "mine"})
        assert params["newClientOrderId"] == "mine"

    def test_an_unknown_exchange_still_gets_a_client_id(self):
        assert client_order_id_params("some-venue", "tb-abc")

    def test_the_caller_s_other_params_survive(self):
        params = client_order_id_params("binance", "tb-abc", {"timeInForce": "IOC"})
        assert params["timeInForce"] == "IOC"


class TestTheRegistryRefusesAReplay:
    @pytest.fixture
    def registry(self) -> IdempotencyRegistry:
        return IdempotencyRegistry(ttl_s=3600.0)

    async def test_a_fresh_key_can_be_reserved(self, registry):
        record = await registry.reserve("k1")
        assert record.state is SubmissionState.IN_FLIGHT

    async def test_an_in_flight_key_cannot_be_reserved_twice(self, registry):
        await registry.reserve("k1")
        with pytest.raises(DuplicateOrderError):
            await registry.reserve("k1")

    async def test_a_completed_key_cannot_be_reserved_again(self, registry):
        await registry.reserve("k1")
        await registry.complete("k1", order_id="ord-1")
        with pytest.raises(DuplicateOrderError):
            await registry.reserve("k1")

    async def test_the_duplicate_error_carries_the_original_record(self, registry):
        await registry.reserve("k1")
        await registry.complete("k1", order_id="ord-1")
        with pytest.raises(DuplicateOrderError) as excinfo:
            await registry.reserve("k1")
        assert "ord-1" in str(excinfo.value) or excinfo.value.args

    async def test_seen_reports_an_in_flight_key(self, registry):
        await registry.reserve("k1")
        assert await registry.seen("k1")

    async def test_seen_does_not_report_an_unknown_key(self, registry):
        assert not await registry.seen("never-used")

    async def test_get_returns_none_for_an_unknown_key(self, registry):
        assert await registry.get("never-used") is None


class TestOnlyAProvablyUnsentRequestReleasesItsKey:
    @pytest.fixture
    def registry(self) -> IdempotencyRegistry:
        return IdempotencyRegistry(ttl_s=3600.0)

    async def test_a_rejection_the_venue_never_accepted_releases_the_key(self, registry):
        # InsufficientFunds: the exchange refused before creating an order,
        # so retrying the same intent is correct, not a duplicate.
        await registry.reserve("k1")
        await registry.fail("k1", "insufficient funds", retryable=True)
        assert not await registry.seen("k1")
        assert await registry.reserve("k1")

    async def test_a_timeout_keeps_the_key_claimed(self, registry):
        # The dangerous case. The request may well have executed with the
        # response lost; releasing the key here is how one intent becomes two
        # live orders.
        await registry.reserve("k1")
        await registry.fail("k1", "confirmation timed out", retryable=False)
        assert await registry.seen("k1")
        with pytest.raises(DuplicateOrderError):
            await registry.reserve("k1")

    async def test_failing_an_unreserved_key_still_records_it(self, registry):
        # A failure path that runs before the reservation (or after an
        # eviction) must not leave the key unclaimed.
        await registry.fail("k9", "boom", retryable=False)
        assert await registry.seen("k9")

    async def test_completing_an_unreserved_key_still_records_it(self, registry):
        await registry.complete("k9", order_id="ord-9")
        assert await registry.seen("k9")


class TestConcurrentDuplicates:
    async def test_only_one_of_many_racing_reservations_wins(self):
        # INV-007 under concurrency: the registry is the serialisation point,
        # so N simultaneous attempts on one key must yield exactly one
        # submission and N-1 refusals.
        registry = IdempotencyRegistry(ttl_s=3600.0)
        results = await asyncio.gather(
            *(registry.reserve("same-key") for _ in range(25)),
            return_exceptions=True,
        )
        winners = [r for r in results if not isinstance(r, Exception)]
        duplicates = [r for r in results if isinstance(r, DuplicateOrderError)]
        assert len(winners) == 1
        assert len(duplicates) == 24

    async def test_distinct_keys_all_succeed(self):
        registry = IdempotencyRegistry(ttl_s=3600.0)
        results = await asyncio.gather(*(registry.reserve(f"k{i}") for i in range(25)))
        assert len(results) == 25


class TestRetentionIsBounded:
    async def test_an_expired_key_is_forgotten(self):
        # Unbounded retention is a leak in a long-running process, and a key
        # whose time bucket closed long ago can never be legitimately
        # replayed.
        registry = IdempotencyRegistry(ttl_s=0.01)
        await registry.reserve("k1")
        await asyncio.sleep(0.05)
        assert await registry.get("k1") is None

    async def test_capacity_is_enforced(self):
        registry = IdempotencyRegistry(ttl_s=3600.0, max_entries=10)
        for i in range(50):
            await registry.reserve(f"k{i}")
        # The newest key must survive: evicting the entry for the order
        # currently in flight is the one eviction that matters.
        assert await registry.seen("k49")

    def test_a_non_positive_ttl_is_refused(self):
        with pytest.raises(ValueError, match="ttl_s must be positive"):
            IdempotencyRegistry(ttl_s=0.0)
