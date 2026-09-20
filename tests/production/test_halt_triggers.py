"""
REL-004 — the halt triggers fire on their own.

Each condition here is one where a human would stop the bot, if they were
watching. They are not watching: these arrive at 4am, and the worst of them
are the quietest. A position that appeared from nowhere raises nothing.
Market data that stopped updating looks exactly like a calm market.

The tests are organised around three properties rather than around the
trigger list: each condition fires individually, a compound incident resolves
to the strictest response rather than the first match, and a trigger that
cannot be evaluated fires rather than passing. The last one is the
counter-intuitive part and the one most likely to be "fixed" by somebody
later, so it is stated loudly.
"""

from __future__ import annotations

import pytest

from src.diagnostics.halt_triggers import (
    MAX_AUTH_FAILURES,
    MAX_CONSECUTIVE_ORDER_FAILURES,
    MAX_DATA_STALENESS_S,
    MAX_DRAWDOWN_FRACTION,
    TRIGGERS,
    Severity,
    evaluate,
    trigger_names,
)

HEALTHY = {
    "market_data_age_s": 5.0,
    "position_deviation": 0.0,
    "reconciliation_ok": True,
    "risk_engine_ok": True,
    "consecutive_order_failures": 0,
    "auth_failures": 0,
    "drawdown_fraction": 0.03,
    "audit_log_ok": True,
    "clock_skew_s": 0.4,
}


def state(**changes):
    return {**HEALTHY, **changes}


class TestAHealthySystemDoesNotHalt:
    def test_nothing_fires(self):
        decision = evaluate(HEALTHY)
        assert decision.should_halt is False
        assert decision.severity is None
        assert decision.report() == "no halt trigger fired"

    def test_every_trigger_is_evaluated_against_healthy_state(self):
        # A trigger that cannot be evaluated fires, so a healthy state passing
        # also proves every predicate found the field it needed.
        assert evaluate(HEALTHY).fired == ()


class TestEachConditionFires:
    @pytest.mark.parametrize(
        "changes,expected",
        [
            ({"position_deviation": 0.5}, "unexpected_position"),
            ({"reconciliation_ok": False}, "reconciliation_failed"),
            ({"market_data_age_s": MAX_DATA_STALENESS_S + 1}, "stale_market_data"),
            ({"risk_engine_ok": False}, "risk_engine_down"),
            ({"drawdown_fraction": MAX_DRAWDOWN_FRACTION}, "drawdown_breached"),
            ({"clock_skew_s": 45.0}, "clock_desynchronised"),
            ({"clock_skew_s": -45.0}, "clock_desynchronised"),
            (
                {"consecutive_order_failures": MAX_CONSECUTIVE_ORDER_FAILURES},
                "repeated_order_failures",
            ),
            ({"auth_failures": MAX_AUTH_FAILURES}, "authentication_failures"),
            ({"audit_log_ok": False}, "audit_log_unwritable"),
        ],
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_the_named_trigger_fires_and_only_it(self, changes, expected):
        decision = evaluate(state(**changes))
        assert {t.name for t in decision.fired} == {expected}

    def test_a_tiny_position_deviation_still_counts(self):
        # Dust is filtered by the reconciler upstream, so anything arriving
        # here is a real discrepancy and a tolerance would only hide it.
        assert evaluate(state(position_deviation=1e-5)).should_halt

    def test_the_clock_check_is_symmetric(self):
        # A clock ahead of the venue's is as fatal as one behind: both make
        # signed requests fall outside the accepted window.
        assert evaluate(state(clock_skew_s=45.0)).should_halt
        assert evaluate(state(clock_skew_s=-45.0)).should_halt


class TestSeverity:
    @pytest.mark.parametrize(
        "changes",
        [
            {"position_deviation": 0.5},
            {"reconciliation_ok": False},
            {"market_data_age_s": 999.0},
            {"risk_engine_ok": False},
            {"drawdown_fraction": 0.9},
        ],
    )
    def test_the_state_corrupting_conditions_halt_everything(self, changes):
        assert evaluate(state(**changes)).severity is Severity.HALT_ALL

    @pytest.mark.parametrize(
        "changes",
        [
            {"consecutive_order_failures": 5},
            {"auth_failures": 5},
            {"audit_log_ok": False},
        ],
    )
    def test_the_operational_conditions_only_stop_new_entries(self, changes):
        # Exits still run: the open positions are real, and stranding them is
        # its own incident.
        assert evaluate(state(**changes)).severity is Severity.HALT_NEW_ENTRIES

    def test_a_compound_incident_takes_the_strictest(self):
        # The failure a first-match-wins chain produces: ordering decides the
        # response, and a reordering silently weakens it.
        decision = evaluate(state(audit_log_ok=False, risk_engine_ok=False))
        assert decision.severity is Severity.HALT_ALL
        assert len(decision.fired) == 2

    def test_every_fired_trigger_is_reported(self):
        decision = evaluate(state(audit_log_ok=False, auth_failures=9, market_data_age_s=999.0))
        assert len(decision.fired) == 3
        for trigger in decision.fired:
            assert trigger.name in decision.report()


class TestAnUnevaluableTriggerFires:
    @pytest.mark.parametrize("missing", sorted(HEALTHY))
    def test_a_missing_field_halts(self, missing):
        # The system knows least about its own state here, which is the worst
        # possible moment to assume it is fine.
        partial = {k: v for k, v in HEALTHY.items() if k != missing}
        assert evaluate(partial).should_halt

    def test_a_malformed_field_halts(self):
        assert evaluate(state(market_data_age_s="unknown")).should_halt

    def test_an_empty_state_halts_everything(self):
        decision = evaluate({})
        assert decision.should_halt
        assert decision.severity is Severity.HALT_ALL
        assert len(decision.fired) == len(TRIGGERS)


class TestTheTriggerSetItself:
    def test_every_trigger_is_justified(self):
        for trigger in TRIGGERS:
            assert len(trigger.rationale) > 40, trigger.name

    def test_trigger_names_are_unique(self):
        assert len(trigger_names()) == len(TRIGGERS)

    def test_the_conditions_from_the_requirement_are_all_present(self):
        # Named individually so a deletion fails by name rather than by an
        # arithmetic mismatch.
        required = {
            "unexpected_position",
            "reconciliation_failed",
            "stale_market_data",
            "risk_engine_down",
            "authentication_failures",
            "audit_log_unwritable",
        }
        assert required <= trigger_names()
