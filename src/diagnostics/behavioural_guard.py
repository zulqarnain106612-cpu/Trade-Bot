"""
REL-007 — a behavioural layer that halts trading that does not look like us.

Every other control in this project checks whether an individual action is
permitted. This one checks whether the *pattern* of actions is the pattern
this bot produces. The two catch different things, and the gap between them
is where the expensive failures live: stolen credentials used correctly, a
model that has quietly started doing something else, a loop that has begun
re-submitting.

Each of those produces perfectly legal orders. Every one passes the risk
gates. What gives them away is the shape — forty orders in a minute from a
bot that places four an hour, a symbol that has never been traded, a 3am
burst from a strategy that only trades the London session.

Two design decisions:

**The profile is declared, not learned.** A learned baseline adapts to an
attacker who moves slowly, which is precisely the attacker worth catching. It
also means the first hour of a compromise becomes the new normal. The numbers
here are what this bot is *supposed* to do, and changing them is a diff.

**Deviation halts rather than alerts.** An alert is a message to somebody who
may be asleep. The cost of a false halt is an operator restarting a bot; the
cost of a false pass is unbounded.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

# The declared envelope. Every number is what normal looks like, with a
# margin -- not a record of the maximum ever observed, which is how a
# threshold ends up just above the incident it failed to catch.
MAX_ORDERS_PER_MINUTE: Final[int] = 10
MAX_ORDERS_PER_HOUR: Final[int] = 60
MAX_NOTIONAL_MULTIPLE: Final[float] = 3.0  # of the trailing median order
MAX_CONSECUTIVE_SAME_DIRECTION: Final[int] = 12
MIN_ORDER_INTERVAL_S: Final[float] = 0.5


@dataclass(frozen=True)
class OrderEvent:
    """One order, reduced to the fields the profile is expressed over."""

    ts: float
    symbol: str
    side: str
    notional_usd: float
    hour_utc: int


@dataclass(frozen=True)
class Profile:
    """What this deployment is supposed to do."""

    allowed_symbols: frozenset[str]
    trading_hours_utc: frozenset[int]
    max_orders_per_minute: int = MAX_ORDERS_PER_MINUTE
    max_orders_per_hour: int = MAX_ORDERS_PER_HOUR
    max_notional_multiple: float = MAX_NOTIONAL_MULTIPLE
    max_consecutive_same_direction: int = MAX_CONSECUTIVE_SAME_DIRECTION
    min_order_interval_s: float = MIN_ORDER_INTERVAL_S


@dataclass(frozen=True)
class Anomaly:
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


@dataclass(frozen=True)
class Verdict:
    anomalies: tuple[Anomaly, ...]

    @property
    def should_halt(self) -> bool:
        return bool(self.anomalies)

    def report(self) -> str:
        if not self.anomalies:
            return "behaviour within profile"
        return "anomalous behaviour: " + "; ".join(str(a) for a in self.anomalies)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def evaluate(events: Sequence[OrderEvent], profile: Profile) -> Verdict:
    """
    Compare a window of orders against the declared profile.

    Every rule is checked; the caller gets the whole list. During an incident
    the combination is the diagnosis -- an unknown symbol *and* a burst is a
    different event from either alone.
    """
    anomalies: list[Anomaly] = []
    if not events:
        return Verdict(())

    ordered = sorted(events, key=lambda e: e.ts)

    unknown = sorted({e.symbol for e in ordered} - profile.allowed_symbols)
    if unknown:
        # The clearest single signal of a stolen credential: the key is valid,
        # the orders are legal, and the symbol is one this bot has never
        # traded.
        anomalies.append(Anomaly("unknown_symbol", f"orders on {unknown}"))

    off_hours = sorted({e.hour_utc for e in ordered} - profile.trading_hours_utc)
    if off_hours:
        anomalies.append(Anomaly("off_hours", f"orders at UTC hours {off_hours}"))

    per_minute = _max_in_window(ordered, 60.0)
    if per_minute > profile.max_orders_per_minute:
        anomalies.append(Anomaly("order_burst", f"{per_minute} orders in a minute"))

    per_hour = _max_in_window(ordered, 3600.0)
    if per_hour > profile.max_orders_per_hour:
        anomalies.append(Anomaly("sustained_rate", f"{per_hour} orders in an hour"))

    gaps = [b.ts - a.ts for a, b in zip(ordered, ordered[1:], strict=False)]
    if gaps and min(gaps) < profile.min_order_interval_s:
        # Two orders 50ms apart is not a strategy decision; it is a retry loop
        # or a duplicate submission.
        anomalies.append(Anomaly("order_interval", f"{min(gaps):.3f}s between two orders"))

    notionals = [e.notional_usd for e in ordered]
    median = _median(notionals)
    if median > 0:
        outsized = [n for n in notionals if n > median * profile.max_notional_multiple]
        if outsized:
            anomalies.append(
                Anomaly(
                    "outsized_order",
                    f"{len(outsized)} order(s) above {median:g} x "
                    f"{profile.max_notional_multiple:g}",
                )
            )

    run = _longest_same_direction_run(ordered)
    if run > profile.max_consecutive_same_direction:
        # A model that has stopped disagreeing with itself, or one input
        # stuck at a constant.
        anomalies.append(Anomaly("directional_run", f"{run} consecutive orders one way"))

    return Verdict(tuple(anomalies))


def _max_in_window(events: Sequence[OrderEvent], window_s: float) -> int:
    """Largest number of events in any sliding window of *window_s*."""
    best = 0
    start = 0
    for end in range(len(events)):
        while events[end].ts - events[start].ts > window_s:
            start += 1
        best = max(best, end - start + 1)
    return best


def _longest_same_direction_run(events: Sequence[OrderEvent]) -> int:
    best = current = 1
    for previous, event in zip(events, events[1:], strict=False):
        current = current + 1 if event.side == previous.side else 1
        best = max(best, current)
    return best
