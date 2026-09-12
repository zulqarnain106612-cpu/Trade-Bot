"""
RES-001 — every component failure has a declared, executable fail-safe.

The usual version of this requirement is a table in a runbook, and the usual
outcome is that the table and the code disagree within two months. Nobody
notices, because the only time anyone reads the table is during an incident,
and during an incident nobody is comparing it to the source.

So the table is here, in code, and the behaviour is read from it rather than
written twice. `on_failure(Component.PRICE_FEED)` returns the declared
response; there is no second place where the price feed's failure is handled
differently, because the handler asks this module.

Two design choices are worth stating:

**The default is HALT.** A component this table has never heard of is one
nobody decided a policy for, and the only safe response to an undecided
failure in a system that spends money is to stop spending it. Adding a
component to the enum without a policy fails the test suite rather than
quietly degrading to "carry on".

**Degradation is explicit and bounded.** Some failures genuinely should not
stop trading -- losing the sentiment feed is not a reason to abandon open
positions -- but "keep going" without a stated bound is how a bot trades for
six hours on a frozen order book. Every DEGRADE carries what is lost and how
long it may last.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class Component(Enum):
    """Every part whose failure has to have an answer."""

    PRICE_FEED = "price_feed"
    ORDER_BOOK_STREAM = "order_book_stream"
    RISK_ENGINE = "risk_engine"
    EXCHANGE_API = "exchange_api"
    EXCHANGE_STATUS = "exchange_status"
    ANALYTICS = "analytics"
    MODEL_INFERENCE = "model_inference"
    AUDIT_LOG = "audit_log"
    DATABASE = "database"
    INTELLIGENCE_FEED = "intelligence_feed"
    SELF_TUNING = "self_tuning"
    API_SERVER = "api_server"


class Response(Enum):
    """What happens when a component fails."""

    # Stop opening positions, keep managing open ones, alert. The default.
    HALT_NEW_ENTRIES = "halt_new_entries"
    # Stop everything, including exits: used only where continuing to act
    # would be worse than not acting at all.
    HALT_ALL = "halt_all"
    # Keep trading with a named capability missing, for a bounded time.
    DEGRADE = "degrade"
    # Retry with backoff; escalates to its `escalates_to` if the bound is hit.
    RETRY = "retry"


@dataclass(frozen=True)
class FailSafe:
    """
    The declared response to one component's failure.

    `lost` and `max_duration_s` are only meaningful for DEGRADE and RETRY, and
    are required there: a degradation with no stated cost and no time bound is
    indistinguishable from ignoring the failure.
    """

    component: Component
    response: Response
    rationale: str
    lost: str = ""
    max_duration_s: float = 0.0
    escalates_to: Response | None = None

    def is_bounded(self) -> bool:
        return self.max_duration_s > 0.0


# The table. Ordered by how bad the failure is, not alphabetically, because
# the ordering is the argument.
_POLICY: Final[dict[Component, FailSafe]] = {
    # Trading on a stale price is worse than not trading. A frozen feed looks
    # identical to a quiet market for exactly as long as it takes to lose
    # money on it.
    Component.PRICE_FEED: FailSafe(
        Component.PRICE_FEED,
        Response.HALT_ALL,
        "A stale price makes every downstream decision wrong, including exits: "
        "closing a position at a price that no longer exists is not a safe exit.",
    ),
    Component.ORDER_BOOK_STREAM: FailSafe(
        Component.ORDER_BOOK_STREAM,
        Response.HALT_NEW_ENTRIES,
        "Without depth the sizing is guesswork, but existing positions can "
        "still be exited on last trade price.",
    ),
    # The risk engine is the thing that says no. Trading with it down is
    # trading with no limits at all.
    Component.RISK_ENGINE: FailSafe(
        Component.RISK_ENGINE,
        Response.HALT_ALL,
        "Every limit this system has is enforced there; without it there is no "
        "bound on a single order or on the book.",
    ),
    Component.EXCHANGE_API: FailSafe(
        Component.EXCHANGE_API,
        Response.RETRY,
        "A venue blip is usually seconds. Unbounded retries against a venue "
        "that is actually down is how duplicate orders appear on recovery.",
        lost="order placement and reconciliation",
        max_duration_s=60.0,
        escalates_to=Response.HALT_ALL,
    ),
    Component.EXCHANGE_STATUS: FailSafe(
        Component.EXCHANGE_STATUS,
        Response.HALT_NEW_ENTRIES,
        "Not knowing whether the venue is in maintenance is not the same as "
        "knowing it is fine.",
    ),
    # The audit log failing does not break trading, and that is exactly why it
    # must halt entries: a period of unaudited trading is unreconstructable.
    Component.AUDIT_LOG: FailSafe(
        Component.AUDIT_LOG,
        Response.HALT_NEW_ENTRIES,
        "Trading that is not recorded cannot be reconstructed afterwards, and "
        "the incident where that matters is the one where it failed.",
    ),
    Component.DATABASE: FailSafe(
        Component.DATABASE,
        Response.RETRY,
        "Position state lives in memory as well, so a short outage is "
        "survivable; a long one means state cannot be persisted and the "
        "process must not keep accumulating unrecorded fills.",
        lost="persistence of new trades and equity points",
        max_duration_s=120.0,
        escalates_to=Response.HALT_ALL,
    ),
    Component.MODEL_INFERENCE: FailSafe(
        Component.MODEL_INFERENCE,
        Response.HALT_NEW_ENTRIES,
        "No model, no signal. Exits are rule-based and continue.",
    ),
    # The genuinely optional ones. Each says what is lost and for how long.
    Component.ANALYTICS: FailSafe(
        Component.ANALYTICS,
        Response.DEGRADE,
        "Attribution and dashboards are observability, not control.",
        lost="attribution, dashboards, drift reporting",
        max_duration_s=3600.0,
        escalates_to=Response.HALT_NEW_ENTRIES,
    ),
    Component.INTELLIGENCE_FEED: FailSafe(
        Component.INTELLIGENCE_FEED,
        Response.DEGRADE,
        "Sentiment and on-chain inputs sharpen a signal; losing them lowers "
        "conviction rather than invalidating the model.",
        lost="sentiment and on-chain features",
        max_duration_s=1800.0,
        escalates_to=Response.HALT_NEW_ENTRIES,
    ),
    Component.SELF_TUNING: FailSafe(
        Component.SELF_TUNING,
        Response.DEGRADE,
        "Parameters stay where they are, which is a safe place for them to be.",
        lost="automatic parameter adaptation",
        max_duration_s=86_400.0,
        escalates_to=Response.HALT_NEW_ENTRIES,
    ),
    Component.API_SERVER: FailSafe(
        Component.API_SERVER,
        Response.DEGRADE,
        "The operator loses visibility and manual control, which matters -- but "
        "halting the strategy because a dashboard is down would itself be an "
        "incident.",
        lost="operator visibility and manual approval",
        max_duration_s=900.0,
        escalates_to=Response.HALT_ALL,
    ),
}

# The response for a component with no declared policy.
DEFAULT_RESPONSE: Final[Response] = Response.HALT_ALL

# Responses under which new positions must not be opened.
_NO_NEW_ENTRIES: Final[frozenset[Response]] = frozenset(
    {Response.HALT_ALL, Response.HALT_NEW_ENTRIES}
)


def on_failure(component: Component) -> FailSafe:
    """
    The declared response to *component* failing.

    An undeclared component resolves to HALT_ALL rather than raising: an
    exception here would be thrown inside a failure handler, which is the
    worst possible place to introduce a second failure.
    """
    declared = _POLICY.get(component)
    if declared is not None:
        return declared
    return FailSafe(
        component,
        DEFAULT_RESPONSE,
        "No policy is declared for this component. An undecided failure in a "
        "system that spends money resolves to stopping.",
    )


def may_open_positions(failed: set[Component]) -> bool:
    """False if any failed component's policy forbids new entries."""
    return not any(on_failure(c).response in _NO_NEW_ENTRIES for c in failed)


def must_halt_all(failed: set[Component]) -> bool:
    """True if any failed component demands a full stop."""
    return any(on_failure(c).response is Response.HALT_ALL for c in failed)


def declared_components() -> frozenset[Component]:
    """Components with an explicit policy. Used by the tests to find gaps."""
    return frozenset(_POLICY)


def policy_table() -> tuple[FailSafe, ...]:
    """The whole table, for rendering into the runbook."""
    return tuple(_POLICY[c] for c in Component if c in _POLICY)
