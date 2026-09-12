"""
The clock policy — one representation of time, stated once.

DATA-003. Trading systems are unusually sensitive to time, and the failure
modes are quiet: a naive datetime compared against an aware one raises, a
naive datetime compared against another naive one silently assumes local time,
and a DST shift moves a bar by an hour without anything reporting an error.

The policy, in full:

1. **Internally, time is an aware UTC ``datetime`` or an integer count of
   milliseconds since the Unix epoch.** Nothing else. A naive ``datetime`` is
   refused at the boundary rather than assumed to mean anything.
2. **Local time never enters a calculation.** It may be produced for display,
   at the edge, by a caller that has decided to.
3. **Exchange time is not our time.** Every venue's clock differs from ours;
   the difference is measured and bounded rather than ignored, and a skew past
   the declared budget is a fault the caller must handle.
4. **There is no DST.** UTC has no daylight saving, which is the entire reason
   the internal representation is UTC rather than a local zone.

The functions here are the only sanctioned way to cross the boundary between
the outside world's time and this project's. They are deliberately small: the
value is in there being exactly one of each.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

#: Default tolerance for exchange-versus-local clock skew. Binance rejects a
#: signed request whose ``recvWindow`` has elapsed, and the usual cause is a
#: host clock that has drifted; five seconds is well inside the venue's
#: tolerance while still catching a host whose NTP has stopped.
DEFAULT_SKEW_BUDGET_MS: int = 5_000


class ClockError(ValueError):
    """Raised when a value is not a time this project is willing to use."""


def utc_now() -> datetime:
    """The current time, aware and in UTC. The only sanctioned 'now'."""
    return datetime.now(UTC)


def utc_now_ms() -> int:
    """The current time as integer milliseconds since the Unix epoch."""
    return int(utc_now().timestamp() * 1000)


def ensure_utc(value: datetime) -> datetime:
    """
    Return ``value`` as an aware UTC datetime, or raise.

    A naive datetime is **refused**, not assumed to be UTC. Assuming is how a
    bar timestamp read from a CSV in a developer's local zone ends up an hour
    or a day away from the bar it describes, with every downstream comparison
    still succeeding.
    """
    if not isinstance(value, datetime):
        raise ClockError(f"expected a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ClockError(
            "naive datetime refused: attach a timezone at the boundary. "
            "A naive value cannot be distinguished from one in the host's "
            "local zone, and this project's internal representation is UTC."
        )
    return value.astimezone(UTC)


def from_epoch_ms(ms: int | float) -> datetime:
    """Convert integer epoch milliseconds to an aware UTC datetime."""
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ClockError(f"epoch milliseconds out of range: {ms!r}") from exc


def to_epoch_ms(value: datetime) -> int:
    """Convert an aware datetime to integer epoch milliseconds."""
    return int(ensure_utc(value).timestamp() * 1000)


@dataclass(frozen=True)
class SkewReport:
    """The measured difference between a venue's clock and ours."""

    #: Venue time minus local time, in milliseconds. Positive means the venue
    #: is ahead of us.
    skew_ms: int
    budget_ms: int

    @property
    def within_budget(self) -> bool:
        return abs(self.skew_ms) <= self.budget_ms

    @property
    def reason(self) -> str:
        if self.within_budget:
            return ""
        direction = "ahead of" if self.skew_ms > 0 else "behind"
        return (
            f"clock skew {abs(self.skew_ms)}ms: the venue is {direction} this host "
            f"by more than the {self.budget_ms}ms budget. Check NTP before trading; "
            f"a signed request built against a drifted clock is rejected by the venue, "
            f"and a bar timestamped against one is misfiled."
        )


def check_exchange_skew(
    exchange_ms: int | float,
    local_ms: int | float | None = None,
    budget_ms: int = DEFAULT_SKEW_BUDGET_MS,
) -> SkewReport:
    """
    Measure venue-versus-local clock skew against a declared budget.

    Returns a report rather than raising: the caller decides whether a drifted
    clock means "refuse to trade" or "log and continue", and that decision
    differs between the order path and a backfill.
    """
    reference = utc_now_ms() if local_ms is None else int(local_ms)
    return SkewReport(skew_ms=int(exchange_ms) - reference, budget_ms=budget_ms)
