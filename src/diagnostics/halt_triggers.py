"""
REL-004 — the halt triggers are machine-enforced, not documented.

Every condition in this module is one where a human would, if they were
watching, stop the bot. The problem is the "if they were watching": these
conditions arrive at 4am, and the ones that matter most are the quietest. A
position that appeared from nowhere does not raise an exception. Market data
that stopped updating looks exactly like a calm market.

So each trigger is a predicate over observable state, evaluated on every
cycle, and the response is not a notification. It is a halt.

Two decisions worth stating:

**Triggers are evaluated independently and the strictest wins.** A compound
incident -- stale data *and* a reconciliation failure -- must not resolve to
the more permissive of the two, which is what a first-match-wins chain would
do depending on ordering.

**An unevaluable trigger fires.** If the check itself cannot run (the data it
needs is missing), that is not a pass. It is exactly the condition where the
system knows least about its own state, which is the worst possible moment to
assume everything is fine.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final


class Severity(Enum):
    """What the trigger does when it fires."""

    HALT_ALL = "halt_all"  # stop everything, including exits
    HALT_NEW_ENTRIES = "halt_new_entries"  # manage what is open, open nothing


@dataclass(frozen=True)
class Trigger:
    name: str
    severity: Severity
    rationale: str
    predicate: Callable[[dict[str, Any]], bool]

    def fires(self, state: dict[str, Any]) -> bool:
        """
        True if this trigger should halt trading.

        A predicate that raises counts as firing. The state it needed was
        missing or malformed, which means the system does not know whether
        the condition holds -- and "unknown" and "fine" are not the same.
        """
        try:
            return bool(self.predicate(state))
        except Exception:  # noqa: BLE001 - see the docstring
            return True


# Thresholds. Each is a number somebody has to defend, rather than a
# sensitivity dial that drifts upward every time it fires.
MAX_DATA_STALENESS_S: Final[float] = 120.0
MAX_CONSECUTIVE_ORDER_FAILURES: Final[int] = 3
MAX_AUTH_FAILURES: Final[int] = 3
MAX_DRAWDOWN_FRACTION: Final[float] = 0.20
MAX_POSITION_DEVIATION: Final[float] = 1e-6


def _stale_market_data(state: dict[str, Any]) -> bool:
    return float(state["market_data_age_s"]) > MAX_DATA_STALENESS_S


def _unexpected_position(state: dict[str, Any]) -> bool:
    # Any position the venue reports and the bot does not know about, or
    # vice versa. Not a tolerance question: dust is filtered upstream by the
    # reconciler, so anything arriving here is a real discrepancy.
    return float(state["position_deviation"]) > MAX_POSITION_DEVIATION


def _reconciliation_failed(state: dict[str, Any]) -> bool:
    return not bool(state["reconciliation_ok"])


def _risk_engine_down(state: dict[str, Any]) -> bool:
    return not bool(state["risk_engine_ok"])


def _repeated_order_failures(state: dict[str, Any]) -> bool:
    return int(state["consecutive_order_failures"]) >= MAX_CONSECUTIVE_ORDER_FAILURES


def _authentication_failures(state: dict[str, Any]) -> bool:
    return int(state["auth_failures"]) >= MAX_AUTH_FAILURES


def _drawdown_breached(state: dict[str, Any]) -> bool:
    return float(state["drawdown_fraction"]) >= MAX_DRAWDOWN_FRACTION


def _audit_log_unwritable(state: dict[str, Any]) -> bool:
    return not bool(state["audit_log_ok"])


def _clock_desynchronised(state: dict[str, Any]) -> bool:
    # Signed requests carry a timestamp and venues reject stale ones; a
    # desynchronised clock turns every order into a rejection, and every
    # rejection into a retry.
    return abs(float(state["clock_skew_s"])) > 30.0


TRIGGERS: Final[tuple[Trigger, ...]] = (
    Trigger(
        "unexpected_position",
        Severity.HALT_ALL,
        "A position nobody placed means the bot's model of the book is wrong. "
        "Acting on a wrong book is worse than not acting.",
        _unexpected_position,
    ),
    Trigger(
        "reconciliation_failed",
        Severity.HALT_ALL,
        "If local and venue state cannot be reconciled, every downstream "
        "number -- exposure, drawdown, available capital -- is unreliable.",
        _reconciliation_failed,
    ),
    Trigger(
        "stale_market_data",
        Severity.HALT_ALL,
        "A frozen feed is indistinguishable from a quiet market for exactly as "
        "long as it takes to lose money on it.",
        _stale_market_data,
    ),
    Trigger(
        "risk_engine_down",
        Severity.HALT_ALL,
        "Every limit the system has is enforced there.",
        _risk_engine_down,
    ),
    Trigger(
        "drawdown_breached",
        Severity.HALT_ALL,
        "The capital-preservation floor. Past it, continuing is a decision a "
        "human should have to make deliberately.",
        _drawdown_breached,
    ),
    Trigger(
        "clock_desynchronised",
        Severity.HALT_ALL,
        "Signed requests carry timestamps. A skewed clock turns every order "
        "into a rejection and every rejection into a retry.",
        _clock_desynchronised,
    ),
    Trigger(
        "repeated_order_failures",
        Severity.HALT_NEW_ENTRIES,
        "Three consecutive failures is a venue or credential problem, not "
        "noise. Exits still run, because the positions are real.",
        _repeated_order_failures,
    ),
    Trigger(
        "authentication_failures",
        Severity.HALT_NEW_ENTRIES,
        "Repeated auth failures mean either a rotated key or somebody else "
        "using it. Both are reasons to stop opening positions.",
        _authentication_failures,
    ),
    Trigger(
        "audit_log_unwritable",
        Severity.HALT_NEW_ENTRIES,
        "Trading that is not recorded cannot be reconstructed afterwards.",
        _audit_log_unwritable,
    ),
)


@dataclass(frozen=True)
class HaltDecision:
    fired: tuple[Trigger, ...]

    @property
    def should_halt(self) -> bool:
        return bool(self.fired)

    @property
    def severity(self) -> Severity | None:
        """The strictest severity among the fired triggers."""
        if not self.fired:
            return None
        if any(t.severity is Severity.HALT_ALL for t in self.fired):
            return Severity.HALT_ALL
        return Severity.HALT_NEW_ENTRIES

    def report(self) -> str:
        if not self.fired:
            return "no halt trigger fired"
        return "halt: " + ", ".join(f"{t.name} ({t.severity.value})" for t in self.fired)


def evaluate(state: dict[str, Any]) -> HaltDecision:
    """
    Evaluate every trigger against *state*.

    All of them, not the first match: a compound incident must resolve to the
    strictest response, and an operator needs the full list rather than
    whichever one happened to be checked first.
    """
    return HaltDecision(fired=tuple(t for t in TRIGGERS if t.fires(state)))


def trigger_names() -> frozenset[str]:
    return frozenset(t.name for t in TRIGGERS)
