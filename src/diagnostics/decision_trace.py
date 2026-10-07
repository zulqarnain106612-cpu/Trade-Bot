"""
Decision traces: the path one tick took, rebuilt from the event bus.

The orchestrator binds a ``trace_id`` for every tick and the bus copies it
onto every event published inside that tick (``Event.context``), so the
signal, the risk-gate verdicts, the order transitions and the fills of one
decision share an id without any producer knowing about tracing. This index
subscribes like any other consumer -- it can fall behind and lose events, and
says so through the subscription's drop counter; it can never slow a
producer (INV-032).

What a trace answers:

* the ordered stages the decision reached;
* the first blocking condition: the first risk gate that refused, or the
  signal's own skip reason when the tick never became tradeable;
* the final decision: FILLED, ORDERED, REJECTED, NO_TRADE or INCOMPLETE.

Engine-level outputs (E-01..E-18) and consensus are summarised in the signal
event rather than published one by one, so a trace shows them as the signal
stage; a per-engine trace needs those producers to publish first.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from src.eventbus import Event, EventEnvelope, Subscription, envelope_of

# Where each topic sits on the decision path. Topics not listed (health,
# intel, runtime, ...) are not part of a trading decision.
STAGE_OF_TOPIC: dict[str, str] = {
    "price": "market_data",
    "book": "market_data",
    "regime": "features",
    "signal": "signal",
    "drift": "signal",
    "gate": "risk",
    "killswitch": "risk",
    "capital_floor": "risk",
    "approval": "approval",
    "order": "order",
    "position": "fill",
}

STAGE_ORDER: tuple[str, ...] = (
    "market_data",
    "features",
    "signal",
    "risk",
    "approval",
    "order",
    "fill",
)


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    trace_id: str
    events: tuple[EventEnvelope, ...]

    @property
    def stages(self) -> tuple[str, ...]:
        seen = {STAGE_OF_TOPIC[e.event_type.split(".")[0]] for e in self.events}
        return tuple(s for s in STAGE_ORDER if s in seen)

    @property
    def request(self) -> EventEnvelope | None:
        """The signal the decision started from."""
        return next((e for e in self.events if e.event_type.startswith("signal")), None)

    @property
    def risk_results(self) -> tuple[EventEnvelope, ...]:
        return tuple(e for e in self.events if e.event_type.startswith("gate"))

    @property
    def first_blocking(self) -> dict[str, Any] | None:
        for e in self.events:
            topic = e.event_type.split(".")[0]
            if topic == "gate" and e.payload.get("passed") is False:
                return {
                    "stage": "risk",
                    "gate": e.payload.get("status"),
                    "reason": e.payload.get("reason"),
                    "event_id": e.event_id,
                }
            if topic == "signal" and e.payload.get("tradeable") is False:
                return {
                    "stage": "signal",
                    "gate": "signal",
                    "reason": e.payload.get("skip_reason"),
                    "event_id": e.event_id,
                }
        return None

    @property
    def final_decision(self) -> str:
        stages = set(self.stages)
        if "fill" in stages:
            return "FILLED"
        if "order" in stages:
            return "ORDERED"
        blocking = self.first_blocking
        if blocking is not None:
            return "REJECTED" if blocking["stage"] == "risk" else "NO_TRADE"
        return "INCOMPLETE"

    def to_dict(self) -> dict[str, Any]:
        request = self.request
        return {
            "trace_id": self.trace_id,
            "stages": list(self.stages),
            "final_decision": self.final_decision,
            "first_blocking": self.first_blocking,
            "request": None if request is None else request.to_dict(),
            "risk_results": [e.to_dict() for e in self.risk_results],
            "events": [e.to_dict() for e in self.events],
        }


class DecisionTraceIndex:
    """
    The most recent ``max_traces`` traces, each capped at ``max_events``.
    Bounded on both axes: a process that runs for weeks keeps the same
    footprint, and the oldest trace is the one evicted.
    """

    def __init__(self, max_traces: int = 500, max_events: int = 200) -> None:
        if max_traces < 1 or max_events < 1:
            raise ValueError("max_traces and max_events must be >= 1")
        self._max_traces = max_traces
        self._max_events = max_events
        self._traces: OrderedDict[str, list[EventEnvelope]] = OrderedDict()
        self.truncated = 0

    def record(self, event: Event) -> bool:
        """Index one event; False when it carries no trace or is off the path."""
        trace_id = event.context.get("trace_id")
        if trace_id is None or event.topic not in STAGE_OF_TOPIC:
            return False
        key = str(trace_id)
        events = self._traces.get(key)
        if events is None:
            events = self._traces[key] = []
            while len(self._traces) > self._max_traces:
                self._traces.popitem(last=False)
        if len(events) >= self._max_events:
            self.truncated += 1
            return False
        events.append(envelope_of(event))
        return True

    async def consume(self, subscription: Subscription) -> None:
        """Feed the index from a bus subscription until it is closed."""
        async for event in subscription:
            self.record(event)

    def trace(self, trace_id: str) -> DecisionTrace | None:
        events = self._traces.get(trace_id)
        return None if events is None else DecisionTrace(trace_id, tuple(events))

    def recent(self, limit: int = 20) -> list[DecisionTrace]:
        ids = list(self._traces)[-limit:]
        return [DecisionTrace(i, tuple(self._traces[i])) for i in reversed(ids)]
