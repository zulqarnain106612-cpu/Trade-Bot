"""
API-008 — a failing security control never opens a trading endpoint.

The failure this guards against is not an attack. It is an outage: the audit
log's disk fills, the rate limiter's state is lost on a restart, an
authorization table fails to load. Every one of those is a condition under
which a system that "degrades gracefully" keeps taking orders with a control
switched off, and nobody notices because nothing errored.

The rule here is the opposite one. A trading endpoint requires its controls to
be *known healthy*. Three states collapse into two:

  healthy      -> the control reported success recently
  degraded     -> the control reported a failure
  never heard  -> treated as degraded

The third line is the one that does the work. A control that was never
registered is not a control that is fine; it is a control nobody can vouch
for, and vouching is the whole job. This is also what makes the registry
resistant to the most likely regression: someone adds a fifth control and
forgets to wire its health reporting, and the endpoints refuse until they do.

Read-only endpoints are deliberately not guarded. Refusing to show an operator
their open positions because the audit log is unhappy removes the information
they need to decide whether to pull the kill switch -- which is itself
available without these controls for the same reason.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Final


class SecurityControl(Enum):
    """The controls a trading endpoint is not allowed to run without."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMITING = "rate_limiting"
    AUDIT_LOGGING = "audit_logging"


# Every member, not a subset: the set a trading endpoint requires is "all of
# them", and spelling it as a separate list is how the two drift apart.
REQUIRED_FOR_TRADING: Final[frozenset[SecurityControl]] = frozenset(SecurityControl)

# What the caller is told. It says a control is unavailable and does not say
# which one -- an attacker who can learn that the audit log is down has been
# handed the window to act in.
UNAVAILABLE_DETAIL: Final[str] = (
    "A required security control is unavailable; trading endpoints are closed."
)


@dataclass(frozen=True)
class ControlStatus:
    control: SecurityControl
    healthy: bool
    reason: str


class SecurityControlUnavailable(RuntimeError):
    """Raised when a trading operation is attempted with a control down."""

    def __init__(self, degraded: frozenset[SecurityControl]) -> None:
        # The exception carries the detail for the server log; the HTTP layer
        # is responsible for not passing it on.
        super().__init__(
            "degraded security controls: " + ", ".join(sorted(c.value for c in degraded))
        )
        self.degraded = degraded


class ControlHealthRegistry:
    """
    Process-wide health of the security controls.

    Locked rather than lock-free: the reads are rare (one per trading
    request) and a torn read here decides whether an order is accepted.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: dict[SecurityControl, ControlStatus] = {}

    def mark_healthy(self, control: SecurityControl) -> None:
        with self._lock:
            self._status[control] = ControlStatus(control, True, "ok")

    def mark_degraded(self, control: SecurityControl, reason: str) -> None:
        with self._lock:
            self._status[control] = ControlStatus(control, False, reason)

    def status(self, control: SecurityControl) -> ControlStatus:
        with self._lock:
            return self._status.get(
                control,
                # The default is a degraded status, not a missing entry, so
                # every caller gets the same shape and none of them has to
                # remember which way "unknown" resolves.
                ControlStatus(control, False, "never reported"),
            )

    def degraded(self) -> frozenset[SecurityControl]:
        """Controls that are unhealthy *or* have never reported."""
        # Read back from the status rather than reusing the loop variable: the
        # status a caller holds must be self-describing, and a status object
        # whose `control` nobody reads is one that could name the wrong
        # control without anything noticing.
        statuses = (self.status(c) for c in REQUIRED_FOR_TRADING)
        return frozenset(s.control for s in statuses if not s.healthy)

    def assert_trading_allowed(self) -> None:
        down = self.degraded()
        if down:
            raise SecurityControlUnavailable(down)

    def reset(self) -> None:
        """Drop every report, returning the registry to fail-closed."""
        with self._lock:
            self._status.clear()


# The process-wide instance the API wires its dependency to.
CONTROL_HEALTH: Final[ControlHealthRegistry] = ControlHealthRegistry()


def mark_all_healthy(registry: ControlHealthRegistry = CONTROL_HEALTH) -> None:
    """
    Declare every control up.

    Called once from `lifespan` after the auth table, the authorization
    matrix, the rate limiter and the audit log have all been constructed --
    i.e. as a statement about startup having got that far, not as a default
    that makes the registry vacuous. A control that later fails calls
    `mark_degraded` and the endpoints close behind it.
    """
    for control in SecurityControl:
        registry.mark_healthy(control)
