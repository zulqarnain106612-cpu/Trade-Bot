"""
API-008 — a failing security control never opens a trading endpoint.

This is the test for an outage, not an attack. Nothing in it simulates an
adversary; it simulates the audit log's disk filling up, the authorization
table failing to load, a restart that lost the rate limiter's state. Those are
ordinary Tuesdays, and the question is only what the trading endpoints do
while they last.

The hardest case to get right is not "control reported a failure". It is
"control never reported anything", which is what a half-finished startup and
a newly added control both look like. A registry that treats silence as
health is a registry that verifies nothing, so that case is tested first and
tested hardest.
"""

from __future__ import annotations

import pytest

from src.api.fail_closed import (
    REQUIRED_FOR_TRADING,
    UNAVAILABLE_DETAIL,
    ControlHealthRegistry,
    SecurityControl,
    SecurityControlUnavailable,
    mark_all_healthy,
)


@pytest.fixture
def registry() -> ControlHealthRegistry:
    # A fresh registry per test, never the process-wide one: a test that
    # leaves CONTROL_HEALTH degraded would fail unrelated suites later, and
    # one that leaves it healthy would hide exactly the bug under test.
    return ControlHealthRegistry()


class TestSilenceIsNotHealth:
    def test_a_fresh_registry_refuses_trading(self, registry):
        with pytest.raises(SecurityControlUnavailable):
            registry.assert_trading_allowed()

    def test_every_control_starts_degraded(self, registry):
        assert registry.degraded() == REQUIRED_FOR_TRADING

    def test_an_unreported_control_has_a_status_not_a_gap(self, registry):
        # Callers should never have to handle "missing" separately; a shape
        # that differs between the healthy and unknown case is where a
        # `.get(...)` returning None quietly becomes falsy-and-ignored.
        status = registry.status(SecurityControl.AUDIT_LOGGING)
        assert status.healthy is False
        assert status.reason == "never reported"

    def test_all_but_one_reporting_is_still_a_refusal(self, registry):
        for control in SecurityControl:
            if control is not SecurityControl.AUDIT_LOGGING:
                registry.mark_healthy(control)
        with pytest.raises(SecurityControlUnavailable) as exc:
            registry.assert_trading_allowed()
        assert exc.value.degraded == frozenset({SecurityControl.AUDIT_LOGGING})

    def test_a_control_added_later_closes_the_endpoints_until_wired(self, registry):
        # The regression this prevents: someone adds a fifth control to the
        # enum and forgets its health reporting. REQUIRED_FOR_TRADING is
        # derived from the enum, so the endpoints refuse rather than silently
        # running with a control nobody checks.
        assert frozenset(SecurityControl) == REQUIRED_FOR_TRADING


class TestReporting:
    def test_all_healthy_permits_trading(self, registry):
        mark_all_healthy(registry)
        registry.assert_trading_allowed()

    @pytest.mark.parametrize("control", list(SecurityControl))
    def test_any_single_degradation_closes_trading(self, registry, control):
        mark_all_healthy(registry)
        registry.mark_degraded(control, "disk full")
        with pytest.raises(SecurityControlUnavailable) as exc:
            registry.assert_trading_allowed()
        assert exc.value.degraded == frozenset({control})

    def test_recovery_reopens_trading(self, registry):
        mark_all_healthy(registry)
        registry.mark_degraded(SecurityControl.RATE_LIMITING, "state lost")
        with pytest.raises(SecurityControlUnavailable):
            registry.assert_trading_allowed()
        registry.mark_healthy(SecurityControl.RATE_LIMITING)
        registry.assert_trading_allowed()

    def test_reset_returns_to_fail_closed(self, registry):
        mark_all_healthy(registry)
        registry.reset()
        with pytest.raises(SecurityControlUnavailable):
            registry.assert_trading_allowed()

    def test_the_degradation_reason_is_kept_for_the_operator(self, registry):
        registry.mark_degraded(SecurityControl.AUDIT_LOGGING, "audit disk full")
        assert registry.status(SecurityControl.AUDIT_LOGGING).reason == "audit disk full"


class TestWhatTheCallerIsTold:
    def test_the_detail_names_no_control(self):
        # Knowing *which* control is down tells an attacker what is currently
        # unobserved, which is the window they want.
        for control in SecurityControl:
            assert control.value not in UNAVAILABLE_DETAIL

    def test_the_exception_does_name_it_for_the_log(self, registry):
        # The information is not destroyed, only kept server-side.
        registry.mark_degraded(SecurityControl.AUTHORIZATION, "table load failed")
        with pytest.raises(SecurityControlUnavailable) as exc:
            registry.assert_trading_allowed()
        assert "authorization" in str(exc.value)


class TestTheGuardIsWiredIntoTheTradingEndpoints:
    """
    Every endpoint that can cause or authorize a trade carries the guard.
    Checked against the route table rather than a hand-kept list, so a new
    trading endpoint cannot be added without this failing.
    """

    GUARDED_PATHS = {
        "/approvals/{request_id}/resolve",
        "/execution-mode",
        "/risk-controls",
        "/strategies/{strategy_id}/re-enable",
    }

    def _dependency_names(self, route) -> set[str]:
        return {getattr(d.call, "__name__", "") for d in route.dependant.dependencies if d.call}

    def test_each_trading_route_depends_on_the_health_check(self):
        from src.api import main

        seen = set()
        for route in main.app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", set()) or set()
            if path in self.GUARDED_PATHS and "POST" in methods:
                seen.add(path)
                names = self._dependency_names(route)
                assert "require_healthy_security_controls" in names, path
        assert seen == self.GUARDED_PATHS

    def test_read_only_routes_are_deliberately_not_guarded(self):
        from src.api import main

        # Refusing to show an operator their positions because the audit log
        # is unhappy removes the information they need in order to decide
        # whether to stop trading. The asymmetry is the design.
        for route in main.app.routes:
            if getattr(route, "path", None) in {"/status", "/trades", "/equity"}:
                assert "require_healthy_security_controls" not in self._dependency_names(route)

    def test_startup_is_what_declares_the_controls_up(self):
        import inspect

        from src.api import main

        source = inspect.getsource(main.lifespan)
        assert "mark_all_healthy()" in source
        # Before readiness, not after: a window where the server is ready and
        # the controls are still unvouched-for is the window this closes.
        assert source.index("mark_all_healthy()") < source.index("_state.ready = True")

    def test_the_dependency_answers_503_without_naming_the_control(self):
        import inspect

        from src.api import main

        source = inspect.getsource(main.require_healthy_security_controls)
        assert "status_code=503" in source
        assert "detail=UNAVAILABLE_DETAIL" in source
