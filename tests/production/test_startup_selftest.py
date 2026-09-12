"""
REL-005, REL-006 — the startup self-test, and configuration drift.

Most production incidents in a system like this are configuration incidents,
and they share a shape: the process starts, reports healthy, and is wrong
about something it never checked. Debug left on. A testnet endpoint in a
production file. A risk limit entered as `20` where `0.20` was meant — a
hundredfold error every downstream calculation accepts without complaint.

None of those raise. All are checkable in the first two seconds. So the tests
below are each one of those incidents, run through the self-test.

The one that is easy to get backwards is `UNEVALUATED`. A check that could
not run has not said no, and it has not said yes either. It blocks, and the
test says so explicitly, because the obvious "simplification" is to treat it
as a pass.
"""

from __future__ import annotations

import pytest

from src.diagnostics.startup_selftest import (
    CHECKS,
    CheckOutcome,
    SelfTestFailed,
    assert_ready_for_live,
    detect_drift,
    run_self_test,
)

HEALTHY = {
    "debug": False,
    "environment": "production",
    "trading_mode": "live",
    "endpoints": ["https://api.binance.com", "wss://stream.binance.com:9443/ws"],
    "required_secrets": ["API_SECRET_KEY", "OPERATOR_SECRET"],
    "present_secrets": ["API_SECRET_KEY", "OPERATOR_SECRET"],
    "risk_limits": {"max_drawdown_pct": 0.20, "max_position_fraction": 0.10},
    "database_ok": True,
    "exchange_auth_ok": True,
    "market_data_age_s": 3.0,
    "models_ok": True,
    "kill_switch_ok": True,
}


def env(**changes):
    return {**HEALTHY, **changes}


class TestAHealthyStartupPasses:
    def test_it_passes(self):
        assert run_self_test(HEALTHY).passed

    def test_every_check_ran(self):
        report = run_self_test(HEALTHY)
        assert len(report.results) == len(CHECKS)
        assert all(r.outcome is CheckOutcome.PASS for r in report.results)

    def test_assert_does_not_raise(self):
        assert_ready_for_live(HEALTHY)


class TestTheConfigurationIncidents:
    def test_debug_left_on_blocks_live(self):
        # The most common one, and the one most likely to expose internals
        # through an error response.
        assert not run_self_test(env(debug=True)).passed

    def test_a_testnet_endpoint_in_production_blocks_live(self):
        assert not run_self_test(env(endpoints=["https://testnet.binance.vision"])).passed

    def test_a_cleartext_endpoint_blocks_live(self):
        assert not run_self_test(env(endpoints=["http://api.binance.com"])).passed

    def test_a_percentage_where_a_fraction_was_meant_blocks_live(self):
        # `20` instead of `0.20`. A hundredfold error that nothing downstream
        # objects to, which is exactly why it is checked here.
        assert not run_self_test(env(risk_limits={"max_drawdown_pct": 20})).passed

    def test_a_zero_risk_limit_blocks_live(self):
        # Zero is not "no limit"; it is a limit nothing can satisfy, and it
        # usually means the value failed to load.
        assert not run_self_test(env(risk_limits={"max_drawdown_pct": 0.0})).passed

    def test_a_missing_secret_blocks_live(self):
        assert not run_self_test(env(present_secrets=["API_SECRET_KEY"])).passed

    def test_the_report_names_secrets_without_printing_them(self):
        report = run_self_test(env(present_secrets=[]))
        rendered = report.report()
        assert "OPERATOR_SECRET" in rendered  # the name
        for value in HEALTHY["present_secrets"]:
            assert f"={value}" not in rendered  # never a value

    def test_a_mode_mismatch_blocks_live(self):
        assert not run_self_test(env(trading_mode="paper")).passed

    @pytest.mark.parametrize(
        "changes",
        [
            {"database_ok": False},
            {"exchange_auth_ok": False},
            {"models_ok": False},
            {"kill_switch_ok": False},
            {"market_data_age_s": 9999.0},
        ],
        ids=lambda c: next(iter(c)),
    )
    def test_each_dependency_failure_blocks_live(self, changes):
        assert not run_self_test(env(**changes)).passed

    def test_an_unreachable_kill_switch_blocks_live(self):
        # The control an operator needs *during* the incident. Discovering it
        # is unreachable at that moment is the worst possible time.
        assert not run_self_test(env(kill_switch_ok=False)).passed


class TestUnevaluatedBlocks:
    def test_a_check_with_no_data_does_not_pass(self):
        missing = {k: v for k, v in HEALTHY.items() if k != "market_data_age_s"}
        report = run_self_test(missing)
        outcomes = {r.name: r.outcome for r in report.results}
        assert outcomes["market_data_fresh"] is CheckOutcome.UNEVALUATED
        assert not report.passed

    def test_unevaluated_is_distinct_from_failed(self):
        # Kept apart so a broken check is visible as broken rather than
        # hiding behind a failing one.
        missing = {k: v for k, v in HEALTHY.items() if k != "market_data_age_s"}
        report = run_self_test(missing)
        assert CheckOutcome.UNEVALUATED in {r.outcome for r in report.results}
        assert CheckOutcome.FAIL not in {r.outcome for r in report.results}

    def test_an_exception_inside_a_check_blocks_rather_than_propagates(self):
        # A self-test that crashes has told the operator nothing, and a crash
        # during startup is often read as "try again".
        report = run_self_test({"risk_limits": {"x": "not a number"}})
        assert not report.passed
        assert any(r.outcome is CheckOutcome.UNEVALUATED for r in report.results)


class TestEveryFailureIsReported:
    def test_the_checks_do_not_short_circuit(self):
        # An operator fixing one configuration problem needs to see the other
        # two now, not on the next three attempts.
        report = run_self_test(env(debug=True, database_ok=False, models_ok=False))
        assert len(report.blockers) == 3

    def test_the_exception_carries_the_detail(self):
        with pytest.raises(SelfTestFailed) as exc:
            assert_ready_for_live(env(debug=True))
        assert "debug_off" in str(exc.value)


class TestConfigurationDrift:
    APPROVED = {"max_drawdown_pct": 0.20, "trading_mode": "live", "debug": False}

    def test_identical_configuration_has_no_drift(self):
        assert detect_drift(self.APPROVED, dict(self.APPROVED)) == ()

    def test_a_changed_value_is_drift(self):
        drift = detect_drift(self.APPROVED, {**self.APPROVED, "max_drawdown_pct": 0.5})
        assert len(drift) == 1
        assert drift[0].key == "max_drawdown_pct"

    def test_an_added_key_is_drift(self):
        # An unreviewed setting: it arrived in the environment rather than in
        # the repository, so nobody looked at it.
        drift = detect_drift(self.APPROVED, {**self.APPROVED, "debug_endpoints": True})
        assert drift and drift[0].key == "debug_endpoints"

    def test_a_removed_key_is_drift(self):
        # A control somebody deleted. Comparing only shared keys would miss
        # this entirely, which is the more dangerous of the two directions.
        actual = {k: v for k, v in self.APPROVED.items() if k != "max_drawdown_pct"}
        drift = detect_drift(self.APPROVED, actual)
        assert drift and drift[0].key == "max_drawdown_pct"

    def test_debug_being_switched_on_shows_as_drift(self):
        drift = detect_drift(self.APPROVED, {**self.APPROVED, "debug": True})
        assert drift and drift[0].actual is True

    def test_the_drift_is_readable(self):
        drift = detect_drift(self.APPROVED, {**self.APPROVED, "trading_mode": "paper"})
        assert "approved 'live'" in str(drift[0])
        assert "running 'paper'" in str(drift[0])
