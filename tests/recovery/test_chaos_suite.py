"""
RES-007 — chaos ends in a safe state, not merely in a handled exception.

The bar for these tests is deliberately higher than "did not crash". A
process that catches every exception and carries on is exactly the failure
this requirement exists to prevent: it keeps trading through a broken
dependency, and the first evidence is a position nobody can explain.

So each scenario below breaks something and then asks the fail-safe policy
what the system is supposed to do about it — the same table the runtime
consults, so a test passing here means the declared behaviour and the actual
behaviour are the same object rather than two things that agree today.

The scenarios are the ones the source document lists: kill the database, kill
the WebSocket, delay and corrupt exchange responses, drop packets, exhaust
memory, saturate CPU, desynchronise the clock. Each is expressed as the
component that failed, because that is the level at which the system has a
policy — and the honest limit of this file is that it exercises the decision,
not the infrastructure. Killing a real database belongs in the nightly chaos
job; deciding what to do when one dies belongs here.
"""

from __future__ import annotations

import pytest

from src.diagnostics.failsafe_policy import (
    Component,
    Response,
    may_open_positions,
    must_halt_all,
    on_failure,
)
from src.diagnostics.recovery_objectives import BookState, DataClass, rpo_for, rto_for

# (scenario, components that fail, whether trading may continue)
SCENARIOS = [
    ("database killed", {Component.DATABASE}, True),
    ("websocket killed", {Component.ORDER_BOOK_STREAM}, False),
    ("price feed frozen", {Component.PRICE_FEED}, False),
    ("exchange api unreachable", {Component.EXCHANGE_API}, True),
    ("exchange status unknown", {Component.EXCHANGE_STATUS}, False),
    ("model inference failing", {Component.MODEL_INFERENCE}, False),
    ("audit log unwritable", {Component.AUDIT_LOG}, False),
    ("analytics down", {Component.ANALYTICS}, True),
    ("intelligence feed down", {Component.INTELLIGENCE_FEED}, True),
    ("self-tuning wedged", {Component.SELF_TUNING}, True),
    ("api server down", {Component.API_SERVER}, True),
]


class TestEveryChaosScenarioHasADecision:
    @pytest.mark.parametrize("name,failed,may_trade", SCENARIOS, ids=[s[0] for s in SCENARIOS])
    def test_the_system_knows_what_to_do(self, name, failed, may_trade):
        assert may_open_positions(failed) is may_trade

    @pytest.mark.parametrize("name,failed,_", SCENARIOS, ids=[s[0] for s in SCENARIOS])
    def test_the_decision_is_reasoned(self, name, failed, _):
        for component in failed:
            assert on_failure(component).rationale

    def test_the_scenario_list_covers_every_component(self):
        # A component nobody wrote a chaos scenario for is one nobody has
        # thought about failing.
        covered = {c for _, failed, _ in SCENARIOS for c in failed}
        assert covered == set(Component)


class TestCompoundFailuresTakeTheStrictestAnswer:
    def test_a_harmless_failure_plus_a_fatal_one_halts(self):
        # The realistic incident is never one thing. A cascade that resolves
        # to the *most permissive* policy is how a system keeps trading
        # through a compound outage.
        assert must_halt_all({Component.ANALYTICS, Component.PRICE_FEED}) is True

    def test_two_degradations_still_allow_trading(self):
        assert may_open_positions({Component.ANALYTICS, Component.INTELLIGENCE_FEED}) is True

    def test_every_subsystem_down_halts_everything(self):
        assert must_halt_all(set(Component)) is True
        assert may_open_positions(set(Component)) is False


class TestTheBoundedFailuresEscalate:
    @pytest.mark.parametrize("component", [Component.EXCHANGE_API, Component.DATABASE])
    def test_a_retry_does_not_run_forever(self, component):
        # Unbounded retries against a venue that is genuinely down is how
        # duplicate orders appear on recovery.
        policy = on_failure(component)
        assert policy.response is Response.RETRY
        assert policy.is_bounded()
        assert policy.escalates_to is Response.HALT_ALL

    @pytest.mark.parametrize(
        "component",
        [
            Component.ANALYTICS,
            Component.INTELLIGENCE_FEED,
            Component.SELF_TUNING,
            Component.API_SERVER,
        ],
    )
    def test_a_degradation_expires_into_a_halt(self, component):
        policy = on_failure(component)
        assert policy.is_bounded()
        assert policy.escalates_to in (Response.HALT_ALL, Response.HALT_NEW_ENTRIES)

    def test_the_bounds_are_ordered_by_how_much_is_lost(self):
        # Losing the exchange API is more urgent than losing the dashboard,
        # and the timeouts should say so.
        assert (
            on_failure(Component.EXCHANGE_API).max_duration_s
            < on_failure(Component.API_SERVER).max_duration_s
            < on_failure(Component.SELF_TUNING).max_duration_s
        )


class TestCorruptedAndDelayedResponses:
    """
    The exchange-response half of the chaos list. The parsing itself is
    covered by the exchange-contract fuzz suite (RES-008); what is asserted
    here is that a response the parser refuses is treated as a *failure of
    the exchange API*, which has a policy, rather than as an empty result.
    """

    def test_a_corrupt_response_is_an_exchange_failure(self):
        from src.execution.exchange_contract import parse_order

        update = parse_order({"id": "ord-1", "status": "\x00garbage"}, expected_order_id="ord-1")
        assert update.needs_reconciliation
        # Needing reconciliation means the order's outcome is unknown, which
        # is the condition the retry-then-halt policy is written for.
        assert on_failure(Component.EXCHANGE_API).response is Response.RETRY

    def test_an_id_mismatch_is_never_treated_as_our_order(self):
        from src.execution.exchange_contract import parse_order

        update = parse_order(
            {"id": "somebody-elses", "status": "closed"}, expected_order_id="ord-1"
        )
        assert update.needs_reconciliation

    def test_a_delayed_response_does_not_release_the_idempotency_key(self):
        # Covered behaviourally in test_crash_replay; restated here because a
        # timeout is the single most common chaos outcome and the temptation
        # to retry it is the single most expensive mistake.
        import asyncio

        from src.execution.idempotency import DuplicateOrderError, IdempotencyRegistry

        async def scenario():
            registry = IdempotencyRegistry(ttl_s=3600.0)
            await registry.reserve("k")
            await registry.fail("k", "timeout waiting for venue", retryable=False)
            with pytest.raises(DuplicateOrderError):
                await registry.reserve("k")

        asyncio.run(scenario())


class TestChaosEndsInsideTheRecoveryObjectives:
    def test_a_database_outage_does_not_relax_the_trade_record_objective(self):
        # The temptation during an outage is to accept "we lost a few
        # minutes". For trade records the declared answer is no.
        assert rpo_for(DataClass.TRADE_RECORDS) == 0.0

    def test_recovery_from_chaos_with_open_positions_is_time_bounded(self):
        assert rto_for(BookState.OPEN_POSITIONS) > 0
        assert rto_for(BookState.OPEN_POSITIONS) < rto_for(BookState.FLAT)
