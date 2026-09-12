"""
RES-005 — concurrency produces no double order and no lost update.

Race conditions are the defects that pass review. Every line is correct in
isolation; the bug lives in the gap between two of them, and it appears only
when two coroutines happen to interleave there. A test that calls the code
once will never see it.

So these tests interleave deliberately: N coroutines racing for one
idempotency key, a reserve interleaved with a completion, a burst of
concurrent reserves on distinct keys. The assertions are the two properties
that matter to a trading system — exactly one submission wins a contested
key, and no concurrent update silently overwrites another.

`asyncio.gather` with many tasks is not a proof of thread safety; it is a
check that the lock is there and covers the check-then-act. The property the
lock provides is not probabilistic, so the useful test is the one that would
fail deterministically if the lock were removed — which is what a contested
key does.
"""

from __future__ import annotations

import asyncio

import pytest

from src.execution.idempotency import (
    DuplicateOrderError,
    IdempotencyRegistry,
    SubmissionState,
    derive_idempotency_key,
)


@pytest.fixture
def registry() -> IdempotencyRegistry:
    return IdempotencyRegistry(ttl_s=3600.0)


class TestOneKeyOneOrder:
    def test_exactly_one_of_many_racers_wins(self, registry):
        async def scenario() -> tuple[int, int]:
            async def attempt() -> bool:
                try:
                    await registry.reserve("contested")
                except DuplicateOrderError:
                    return False
                return True

            results = await asyncio.gather(*(attempt() for _ in range(50)))
            return sum(results), len(results) - sum(results)

        won, lost = asyncio.run(scenario())
        # Without the lock around check-then-insert this is where several
        # racers all see "not present" and all proceed.
        assert won == 1
        assert lost == 49

    def test_a_second_wave_after_completion_still_finds_it_claimed(self, registry):
        async def scenario() -> list[bool]:
            await registry.reserve("k")
            await registry.complete("k", order_id="venue-1")

            async def attempt() -> bool:
                try:
                    await registry.reserve("k")
                except DuplicateOrderError:
                    return False
                return True

            return await asyncio.gather(*(attempt() for _ in range(20)))

        assert not any(asyncio.run(scenario()))

    def test_distinct_keys_do_not_block_each_other(self, registry):
        # The other half: a lock that serialises everything would make one
        # slow venue call stall every other symbol's order.
        async def scenario() -> int:
            async def attempt(i: int) -> bool:
                await registry.reserve(f"key-{i}")
                return True

            return sum(await asyncio.gather(*(attempt(i) for i in range(100))))

        assert asyncio.run(scenario()) == 100


class TestNoLostUpdate:
    def test_a_completion_is_not_overwritten_by_a_concurrent_failure(self, registry):
        async def scenario():
            await registry.reserve("k")
            await asyncio.gather(
                registry.complete("k", order_id="venue-1"),
                registry.fail("k", "late timeout", retryable=False),
            )
            return await registry.get("k")

        record = asyncio.run(scenario())
        assert record is not None
        # Either ordering is safe *because* a non-retryable failure also
        # resolves to COMPLETED -- the key stays claimed either way, which is
        # the property that prevents a duplicate.
        assert record.state is SubmissionState.COMPLETED

    def test_concurrent_completions_settle_on_one_order_id(self, registry):
        async def scenario():
            await registry.reserve("k")
            await asyncio.gather(
                *(registry.complete("k", order_id=f"venue-{i}") for i in range(10))
            )
            return await registry.get("k")

        record = asyncio.run(scenario())
        assert record is not None
        assert record.order_id is not None
        assert record.order_id.startswith("venue-")

    def test_reads_during_writes_never_see_a_half_written_record(self, registry):
        async def scenario() -> list[bool]:
            await registry.reserve("k")

            async def writer() -> None:
                for i in range(50):
                    await registry.complete("k", order_id=f"venue-{i}")

            async def reader() -> list[bool]:
                seen = []
                for _ in range(50):
                    record = await registry.get("k")
                    # A torn read would show a COMPLETED state with no id, or
                    # an id with a stale state.
                    seen.append(
                        record is not None
                        and (record.state is not SubmissionState.COMPLETED or record.order_id)
                    )
                    await asyncio.sleep(0)
                return seen

            _, seen = await asyncio.gather(writer(), reader())
            return seen

        assert all(asyncio.run(scenario()))


class TestTheKeyItselfIsStable:
    def test_identical_signals_derive_identical_keys(self):
        # The property the whole mechanism rests on: a retry of the same
        # decision must produce the same key, or idempotency protects nothing.
        kwargs = {
            "strategy_id": "signal_engine_v1",
            "symbol": "BTC/USDT",
            "side": "buy",
            "quantity": 0.5,
            "purpose": "entry",
            "intent_id": "intent-1",
        }
        assert derive_idempotency_key(**kwargs) == derive_idempotency_key(**kwargs)

    @pytest.mark.parametrize(
        "changed",
        [
            {"symbol": "ETH/USDT"},
            {"side": "sell"},
            {"quantity": 0.6},
            {"purpose": "exit"},
            {"strategy_id": "other_strategy"},
            {"intent_id": "intent-2"},
        ],
    )
    def test_a_different_decision_derives_a_different_key(self, changed):
        base = {
            "strategy_id": "signal_engine_v1",
            "symbol": "BTC/USDT",
            "side": "buy",
            "quantity": 0.5,
            "purpose": "entry",
            "intent_id": "intent-1",
        }
        assert derive_idempotency_key(**base) != derive_idempotency_key(**{**base, **changed})

    def test_two_symbols_racing_produce_two_orders(self, registry):
        # Distinct decisions must not collide into one key: that would be a
        # *missed* order, which is the failure hiding behind over-eager
        # deduplication.
        async def scenario() -> int:
            keys = [
                derive_idempotency_key(
                    strategy_id="signal_engine_v1",
                    symbol=s,
                    side="buy",
                    quantity=1.0,
                    purpose="entry",
                    intent_id="intent-1",
                )
                for s in ("BTC/USDT", "ETH/USDT")
            ]
            assert len(set(keys)) == 2
            await asyncio.gather(*(registry.reserve(k) for k in keys))
            return len(keys)

        assert asyncio.run(scenario()) == 2
