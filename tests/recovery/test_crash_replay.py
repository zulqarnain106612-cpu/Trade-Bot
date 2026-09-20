"""
RES-002, INV-009 — kill the process at every stage of an order, then restart.

The order lifecycle has a small number of moments, and the process can die in
any of them. Most are harmless. Two are not, and they are opposites:

  * dying **after** the venue accepted the order but **before** the local
    record exists produces a position nobody knows about — the system trades
    on a book that is wrong, and risk limits are computed against a fiction;
  * dying **after** the local record exists but **before** the venue accepted
    produces a phantom position — the system thinks it is exposed when it is
    not, and may "close" something that never opened.

Recovery cannot tell those apart from local state alone, which is the whole
argument for reconciling against the venue at startup. So these tests walk
the lifecycle stage by stage, kill at each one, and require the restart to
reach a state the reconciler calls consistent — or, where it cannot, to
report the discrepancy rather than average it away.

The idempotency registry is exercised in the same file because it is what
makes the replay safe: a retry after an ambiguous failure must not become a
second order.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum

import pytest

from src.diagnostics.disaster_recovery import (
    DiscrepancyType,
    PositionSnapshot,
    is_state_consistent,
    reconcile,
)
from src.execution.idempotency import (
    DuplicateOrderError,
    IdempotencyRegistry,
    SubmissionState,
)


class Stage(Enum):
    """Where the process died, in lifecycle order."""

    BEFORE_RESERVE = "before_reserve"
    AFTER_RESERVE = "after_reserve"
    AFTER_VENUE_ACCEPT = "after_venue_accept"
    AFTER_LOCAL_RECORD = "after_local_record"
    AFTER_COMPLETE = "after_complete"


@dataclass
class World:
    """The two places state lives, and the gap between them."""

    venue: dict[str, float] = field(default_factory=dict)
    local: dict[str, float] = field(default_factory=dict)

    def venue_snapshot(self) -> list[PositionSnapshot]:
        return [PositionSnapshot(s, q) for s, q in sorted(self.venue.items())]

    def local_snapshot(self) -> list[PositionSnapshot]:
        return [PositionSnapshot(s, q) for s, q in sorted(self.local.items())]

    def discrepancies(self):
        return reconcile(self.local_snapshot(), self.venue_snapshot())


async def place_order_until(stage: Stage, world: World, registry: IdempotencyRegistry, key: str):
    """
    Walk the order lifecycle and stop at *stage*, as a crash would.

    Written as one function with early returns rather than five fixtures so
    the ordering is visible: the sequence is the thing under test.
    """
    if stage is Stage.BEFORE_RESERVE:
        return

    await registry.reserve(key)
    if stage is Stage.AFTER_RESERVE:
        return

    world.venue["BTC/USDT"] = world.venue.get("BTC/USDT", 0.0) + 0.5
    if stage is Stage.AFTER_VENUE_ACCEPT:
        return

    world.local["BTC/USDT"] = world.local.get("BTC/USDT", 0.0) + 0.5
    if stage is Stage.AFTER_LOCAL_RECORD:
        return

    await registry.complete(key, order_id="venue-1")


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def registry() -> IdempotencyRegistry:
    return IdempotencyRegistry(ttl_s=3600.0)


class TestEveryCrashPointLeavesAReadableState:
    @pytest.mark.parametrize("stage", list(Stage))
    def test_reconciliation_runs_and_returns_a_verdict(self, stage, world, registry):
        # The first requirement is weaker than "consistent" and more
        # important: recovery must not crash, whatever it finds.
        asyncio.run(place_order_until(stage, world, registry, "key-1"))
        discrepancies = world.discrepancies()
        assert isinstance(discrepancies, list)

    @pytest.mark.parametrize(
        "stage", [Stage.BEFORE_RESERVE, Stage.AFTER_RESERVE, Stage.AFTER_LOCAL_RECORD]
    )
    def test_the_safe_stages_are_consistent(self, stage, world, registry):
        asyncio.run(place_order_until(stage, world, registry, "key-1"))
        assert is_state_consistent(world.discrepancies())

    def test_a_crash_after_completion_is_consistent(self, world, registry):
        asyncio.run(place_order_until(Stage.AFTER_COMPLETE, world, registry, "key-1"))
        assert is_state_consistent(world.discrepancies())


class TestTheDangerousGapIsReportedNotHidden:
    def test_a_venue_fill_with_no_local_record_is_found(self, world, registry):
        # The expensive one: a real position the system does not know it
        # holds. Every risk limit downstream is computed against a book that
        # is missing it.
        asyncio.run(place_order_until(Stage.AFTER_VENUE_ACCEPT, world, registry, "key-1"))
        found = world.discrepancies()
        assert not is_state_consistent(found)
        assert found[0].discrepancy_type is DiscrepancyType.MISSING_LOCALLY
        assert found[0].reference_quantity == pytest.approx(0.5)
        assert found[0].local_quantity == 0.0

    def test_a_phantom_local_position_is_found(self, world):
        # The opposite failure: local state says exposed, the venue says flat.
        world.local["ETH/USDT"] = 2.0
        found = world.discrepancies()
        assert found[0].discrepancy_type is DiscrepancyType.MISSING_IN_REFERENCE

    def test_a_partial_fill_is_a_quantity_mismatch_not_a_missing_position(self, world):
        # Recovery has to distinguish "some of it filled" from "none of it
        # did"; treating a partial fill as absent would re-place the whole
        # order on top of the part that exists.
        world.venue["BTC/USDT"] = 0.3
        world.local["BTC/USDT"] = 0.5
        found = world.discrepancies()
        assert found[0].discrepancy_type is DiscrepancyType.QUANTITY_MISMATCH

    def test_reconciliation_reports_rather_than_correcting(self, world, registry):
        # The reconciler is deliberately pure: it must not place a corrective
        # order on its own, because an automated correction against a venue
        # that is merely slow to report is how a position gets doubled.
        asyncio.run(place_order_until(Stage.AFTER_VENUE_ACCEPT, world, registry, "key-1"))
        before = dict(world.venue)
        world.discrepancies()
        assert world.venue == before


class TestTheReplayCannotDoubleTheOrder:
    def test_replaying_a_reserved_key_is_refused(self, world, registry):
        async def scenario():
            await place_order_until(Stage.AFTER_VENUE_ACCEPT, world, registry, "key-1")
            # Restart: the same signal, the same inputs, so the same key.
            with pytest.raises(DuplicateOrderError):
                await registry.reserve("key-1")

        asyncio.run(scenario())
        assert world.venue["BTC/USDT"] == pytest.approx(0.5)

    def test_an_ambiguous_failure_keeps_the_key_claimed(self, registry):
        # A timeout is not proof that nothing happened. Releasing the key
        # would let the retry place a second order against a first one that
        # may well have executed.
        async def scenario():
            await registry.reserve("key-2")
            await registry.fail("key-2", "read timeout", retryable=False)
            assert await registry.seen("key-2") is True
            with pytest.raises(DuplicateOrderError):
                await registry.reserve("key-2")

        asyncio.run(scenario())

    def test_a_provably_failed_submission_is_retryable(self, registry):
        # The other direction: a rejection the venue explicitly returned did
        # not create an order, so refusing the retry would strand the signal.
        async def scenario():
            await registry.reserve("key-3")
            await registry.fail("key-3", "insufficient balance", retryable=True)
            record = await registry.get("key-3")
            assert record is not None and record.state is SubmissionState.FAILED
            await registry.reserve("key-3")  # must not raise

        asyncio.run(scenario())

    def test_completion_is_recorded_with_the_venue_id(self, registry):
        async def scenario():
            await registry.reserve("key-4")
            await registry.complete("key-4", order_id="venue-9")
            record = await registry.get("key-4")
            assert record is not None
            assert record.state is SubmissionState.COMPLETED
            assert record.order_id == "venue-9"

        asyncio.run(scenario())


class TestStateReconstructionMatchesTheVenue:
    """INV-009 — after a restart, reconstructed state equals the exchange's."""

    def test_a_clean_restart_reconstructs_exactly(self, world):
        world.venue = {"BTC/USDT": 0.5, "ETH/USDT": -2.0}
        world.local = {"BTC/USDT": 0.5, "ETH/USDT": -2.0}
        assert is_state_consistent(world.discrepancies())

    def test_a_short_position_is_not_confused_with_a_long_one(self, world):
        # Signed quantities: a sign error here would read a short as a long
        # and compute every risk number backwards.
        world.venue = {"ETH/USDT": -2.0}
        world.local = {"ETH/USDT": 2.0}
        found = world.discrepancies()
        assert found and found[0].discrepancy_type is DiscrepancyType.QUANTITY_MISMATCH

    def test_dust_does_not_count_as_a_discrepancy(self, world):
        # Float arithmetic on fills leaves residue. Flagging it would make
        # every restart report a false discrepancy, and a check that cries
        # wolf on every restart is one nobody reads.
        world.venue = {"BTC/USDT": 0.5}
        world.local = {"BTC/USDT": 0.5 + 1e-12}
        assert is_state_consistent(world.discrepancies())

    def test_an_empty_book_on_both_sides_is_consistent(self, world):
        assert is_state_consistent(world.discrepancies())

    def test_every_symbol_is_checked_not_just_the_first(self, world):
        world.venue = {"BTC/USDT": 1.0, "ETH/USDT": 1.0, "SOL/USDT": 1.0}
        world.local = {"BTC/USDT": 1.0}
        found = world.discrepancies()
        assert {d.symbol for d in found} == {"ETH/USDT", "SOL/USDT"}
