"""
RES-001 — every component failure has a declared fail-safe, and it is the
one the code actually takes.

The usual failure here is drift: a table in a runbook that stops matching the
handler six weeks after both were written. Nobody notices, because the only
person who reads the runbook is reading it during an incident and is not
diffing it against the source.

So the assertions below are about completeness and about direction. Every
component in the enum has a policy; every policy that keeps trading says what
is lost and for how long; and the default for anything undeclared is to stop.
That last one is the property that makes adding a component safe: forget the
policy and the system halts, rather than trading through a failure nobody
decided about.
"""

from __future__ import annotations

import pytest

from src.diagnostics.failsafe_policy import (
    DEFAULT_RESPONSE,
    Component,
    FailSafe,
    Response,
    declared_components,
    may_open_positions,
    must_halt_all,
    on_failure,
    policy_table,
)


class TestTheTableIsComplete:
    @pytest.mark.parametrize("component", list(Component))
    def test_every_component_has_a_policy(self, component):
        assert component in declared_components(), (
            f"{component.value} has no declared fail-safe; the system will halt "
            "on its failure, which is safe but undecided"
        )

    @pytest.mark.parametrize("component", list(Component))
    def test_every_policy_carries_a_rationale(self, component):
        # A policy with no reasoning is one nobody can evaluate when it turns
        # out to be wrong at 3am.
        assert len(on_failure(component).rationale) > 40

    def test_the_table_covers_the_enum_exactly(self):
        assert {f.component for f in policy_table()} == set(Component)


class TestDegradationIsBounded:
    @pytest.mark.parametrize(
        "policy", [p for p in policy_table() if p.response is Response.DEGRADE]
    )
    def test_a_degrade_says_what_is_lost(self, policy: FailSafe):
        # "Keep going" without naming the missing capability is how a bot
        # trades for six hours on a frozen sentiment feed.
        assert policy.lost, f"{policy.component.value} degrades without saying what is lost"

    @pytest.mark.parametrize(
        "policy", [p for p in policy_table() if p.response is Response.DEGRADE]
    )
    def test_a_degrade_is_time_bounded(self, policy: FailSafe):
        assert policy.is_bounded(), f"{policy.component.value} degrades indefinitely"

    @pytest.mark.parametrize(
        "policy", [p for p in policy_table() if p.response is Response.DEGRADE]
    )
    def test_a_degrade_escalates(self, policy: FailSafe):
        # The bound has to lead somewhere. A timer that expires into nothing
        # is a timer nobody set.
        assert policy.escalates_to is not None

    @pytest.mark.parametrize("policy", [p for p in policy_table() if p.response is Response.RETRY])
    def test_a_retry_is_bounded_and_escalates(self, policy: FailSafe):
        # Unbounded retries against a venue that is genuinely down is how
        # duplicate orders appear on recovery.
        assert policy.is_bounded()
        assert policy.escalates_to in (Response.HALT_ALL, Response.HALT_NEW_ENTRIES)

    @pytest.mark.parametrize(
        "policy", [p for p in policy_table() if p.response in (Response.HALT_ALL,)]
    )
    def test_a_halt_does_not_pretend_to_be_bounded(self, policy: FailSafe):
        assert policy.escalates_to is None


class TestTheDangerousComponentsHalt:
    @pytest.mark.parametrize(
        "component",
        [Component.PRICE_FEED, Component.RISK_ENGINE],
    )
    def test_they_stop_everything(self, component):
        # The price feed because a stale price makes exits wrong too, and the
        # risk engine because it is the only thing that says no.
        assert on_failure(component).response is Response.HALT_ALL

    def test_the_audit_log_stops_new_entries(self, component=Component.AUDIT_LOG):
        # Trading that is not recorded cannot be reconstructed, and the
        # incident where that matters is the one where the log failed.
        assert on_failure(component).response is Response.HALT_NEW_ENTRIES

    @pytest.mark.parametrize(
        "component",
        [Component.ANALYTICS, Component.INTELLIGENCE_FEED, Component.SELF_TUNING],
    )
    def test_observability_and_tuning_do_not_stop_trading(self, component):
        # The other direction matters too: a policy that halts on a dashboard
        # outage is one somebody will switch off.
        assert on_failure(component).response is Response.DEGRADE


class TestTheDefaultIsToStop:
    def test_the_declared_default(self):
        assert DEFAULT_RESPONSE is Response.HALT_ALL

    def test_an_undeclared_component_halts(self):
        # Simulated by removing a component from the table, which is what a
        # newly added enum member looks like before somebody writes its
        # policy -- the case this default exists for.
        from src.diagnostics import failsafe_policy

        saved = failsafe_policy._POLICY.pop(Component.ANALYTICS)
        try:
            fallback = on_failure(Component.ANALYTICS)
            assert fallback.response is Response.HALT_ALL
            assert "No policy is declared" in fallback.rationale
        finally:
            failsafe_policy._POLICY[Component.ANALYTICS] = saved

    def test_the_lookup_never_raises(self):
        # This runs inside a failure handler. An exception here would be a
        # second failure at the worst possible moment.
        from src.diagnostics import failsafe_policy

        saved = dict(failsafe_policy._POLICY)
        failsafe_policy._POLICY.clear()
        try:
            for component in Component:
                assert on_failure(component).response is Response.HALT_ALL
        finally:
            failsafe_policy._POLICY.update(saved)


class TestTheAggregateHelpers:
    def test_nothing_failed_means_trading_continues(self):
        assert may_open_positions(set()) is True
        assert must_halt_all(set()) is False

    def test_a_degrading_failure_alone_does_not_stop_entries(self):
        assert may_open_positions({Component.ANALYTICS}) is True

    def test_one_halting_failure_stops_entries(self):
        assert may_open_positions({Component.ANALYTICS, Component.RISK_ENGINE}) is False

    def test_halt_all_is_distinguished_from_halt_new_entries(self):
        # Exits must still run when only entries are halted -- conflating the
        # two would strand open positions on an audit-log outage.
        assert must_halt_all({Component.AUDIT_LOG}) is False
        assert may_open_positions({Component.AUDIT_LOG}) is False
        assert must_halt_all({Component.PRICE_FEED}) is True

    def test_multiple_failures_take_the_strictest(self):
        failed = {Component.ANALYTICS, Component.AUDIT_LOG, Component.PRICE_FEED}
        assert must_halt_all(failed) is True
