"""
REL-005, REL-006 — the startup self-test, and the drift it exists to catch.

Most production incidents in a system like this are configuration incidents,
and they share a shape: the process starts, reports healthy, and is wrong
about something it never checked. Debug mode left on. A testnet endpoint in a
production environment file. A risk limit an order of magnitude off after
somebody typed a percentage where a fraction was expected.

None of those raise. All of them are checkable in the first two seconds.

So this runs before the bot is allowed to trade, and **any** failure blocks
live mode. Not a warning, not a degraded start: the conditions below are the
ones where continuing means trading with an assumption that has already been
shown to be false.

`ConfigDrift` is the second half. Where the self-test asks "is this
configuration valid", drift asks "is this the configuration we approved" --
and those differ exactly when somebody changed something in the environment
rather than in the repository, which is the case nobody reviews.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final


class CheckOutcome(Enum):
    PASS = "pass"
    FAIL = "fail"
    # Deliberately distinct from FAIL: a check that could not run has not
    # said no, and treating the two the same hides a broken check behind a
    # failing one.
    UNEVALUATED = "unevaluated"


@dataclass(frozen=True)
class CheckResult:
    name: str
    outcome: CheckOutcome
    detail: str = ""

    @property
    def blocks_live(self) -> bool:
        # UNEVALUATED blocks too. The whole point of the self-test is to
        # establish facts before trading; a fact it could not establish is
        # not a fact it may assume.
        return self.outcome is not CheckOutcome.PASS


@dataclass(frozen=True)
class SelfTestReport:
    results: tuple[CheckResult, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return not any(r.blocks_live for r in self.results)

    @property
    def blockers(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if r.blocks_live)

    def report(self) -> str:
        if self.passed:
            return f"startup self-test passed ({len(self.results)} checks)"
        return "startup self-test failed:\n  " + "\n  ".join(
            f"{r.name}: {r.outcome.value} -- {r.detail}" for r in self.blockers
        )


class SelfTestFailed(RuntimeError):
    """Live trading was attempted after a failing startup self-test."""


# The checks, as (name, predicate) over an environment snapshot. Predicates
# rather than direct I/O so the whole suite is testable without a venue, a
# database or a network -- a self-test that can only be exercised in
# production is one nobody exercises.
Check = Callable[[dict[str, Any]], CheckResult]


def _result(name: str, ok: bool, detail: str) -> CheckResult:
    return CheckResult(name, CheckOutcome.PASS if ok else CheckOutcome.FAIL, "" if ok else detail)


def check_debug_is_off(env: dict[str, Any]) -> CheckResult:
    # The single most common production configuration defect, and the one
    # most likely to expose internals through an error response.
    return _result(
        "debug_off",
        not bool(env.get("debug", False)),
        "debug mode is enabled; error responses may carry internals",
    )


def check_trading_mode_matches_environment(env: dict[str, Any]) -> CheckResult:
    return _result(
        "trading_mode_matches",
        env.get("environment") != "production" or env.get("trading_mode") == "live",
        f"environment is {env.get('environment')!r} but trading_mode is "
        f"{env.get('trading_mode')!r}",
    )


def check_endpoints_are_production(env: dict[str, Any]) -> CheckResult:
    endpoints = env.get("endpoints") or []
    suspicious = [e for e in endpoints if any(m in str(e) for m in ("testnet", "sandbox", "mock"))]
    return _result(
        "endpoints_are_production",
        not suspicious,
        f"non-production endpoints configured: {suspicious}",
    )


def check_tls_everywhere(env: dict[str, Any]) -> CheckResult:
    from src.security.tls import is_secure_url

    endpoints = env.get("endpoints") or []
    cleartext = [e for e in endpoints if not is_secure_url(str(e))]
    return _result("tls_everywhere", not cleartext, f"cleartext endpoints: {cleartext}")


def check_secrets_present(env: dict[str, Any]) -> CheckResult:
    required = env.get("required_secrets") or []
    present = env.get("present_secrets") or []
    missing = sorted(set(required) - set(present))
    # Names only. A self-test that prints a secret to prove it is present has
    # defeated the thing it was checking.
    return _result("secrets_present", not missing, f"missing secrets: {missing}")


def check_risk_limits_are_sane(env: dict[str, Any]) -> CheckResult:
    limits = env.get("risk_limits") or {}
    problems = [
        f"{name}={value}"
        for name, value in limits.items()
        # Fractions, not percentages. A 20 where 0.20 was meant is a hundredfold
        # error that every downstream calculation silently accepts.
        if not (0.0 < float(value) <= 1.0)
    ]
    return _result("risk_limits_sane", not problems, f"limits outside (0, 1]: {problems}")


def check_database_reachable(env: dict[str, Any]) -> CheckResult:
    return _result("database_reachable", bool(env.get("database_ok")), "database is unreachable")


def check_exchange_authenticated(env: dict[str, Any]) -> CheckResult:
    return _result(
        "exchange_authenticated",
        bool(env.get("exchange_auth_ok")),
        "exchange authentication failed",
    )


def check_market_data_fresh(env: dict[str, Any]) -> CheckResult:
    from src.diagnostics.halt_triggers import MAX_DATA_STALENESS_S

    age = env.get("market_data_age_s")
    if age is None:
        return CheckResult(
            "market_data_fresh",
            CheckOutcome.UNEVALUATED,
            "no market-data age was reported",
        )
    return _result(
        "market_data_fresh",
        float(age) <= MAX_DATA_STALENESS_S,
        f"market data is {age}s old",
    )


def check_model_artifacts_loadable(env: dict[str, Any]) -> CheckResult:
    return _result(
        "model_artifacts_loadable",
        bool(env.get("models_ok")),
        "one or more model artifacts failed to load",
    )


def check_kill_switch_reachable(env: dict[str, Any]) -> CheckResult:
    # The control an operator needs *during* the incident. Discovering it is
    # unreachable at that moment is the worst possible time.
    return _result(
        "kill_switch_reachable",
        bool(env.get("kill_switch_ok")),
        "the kill switch did not respond",
    )


CHECKS: Final[tuple[Check, ...]] = (
    check_debug_is_off,
    check_trading_mode_matches_environment,
    check_endpoints_are_production,
    check_tls_everywhere,
    check_secrets_present,
    check_risk_limits_are_sane,
    check_database_reachable,
    check_exchange_authenticated,
    check_market_data_fresh,
    check_model_artifacts_loadable,
    check_kill_switch_reachable,
)


def run_self_test(env: dict[str, Any]) -> SelfTestReport:
    """
    Run every check. No short-circuit, for the usual reason: an operator
    fixing one configuration problem needs to see the other two now.
    """
    results: list[CheckResult] = []
    for check in CHECKS:
        try:
            results.append(check(env))
        except Exception as exc:  # noqa: BLE001 - an unevaluable check blocks
            results.append(
                CheckResult(
                    getattr(check, "__name__", "unknown"),
                    CheckOutcome.UNEVALUATED,
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return SelfTestReport(results=tuple(results))


def assert_ready_for_live(env: dict[str, Any]) -> None:
    report = run_self_test(env)
    if not report.passed:
        raise SelfTestFailed(report.report())


# ---------------------------------------------------------------------------
# REL-006 -- configuration drift
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Drift:
    key: str
    approved: Any
    actual: Any

    def __str__(self) -> str:
        return f"{self.key}: approved {self.approved!r}, running {self.actual!r}"


def detect_drift(approved: dict[str, Any], actual: dict[str, Any]) -> tuple[Drift, ...]:
    """
    Compare the running configuration against the approved one.

    Both directions matter. A key that appeared is an unreviewed setting; a
    key that vanished is a control somebody removed. Reporting only
    differences in shared keys would miss both.
    """
    drifts: list[Drift] = []
    for key in sorted(set(approved) | set(actual)):
        if key not in actual:
            drifts.append(Drift(key, approved[key], "<absent>"))
        elif key not in approved:
            drifts.append(Drift(key, "<unapproved>", actual[key]))
        elif approved[key] != actual[key]:
            drifts.append(Drift(key, approved[key], actual[key]))
    return tuple(drifts)
