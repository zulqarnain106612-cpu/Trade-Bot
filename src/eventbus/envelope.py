"""
The universal event envelope.

Every ``Event`` already carries what the envelope needs: its id, topic,
producer timestamp, payload, and the correlation ids bound where it was
published (``Event.context``, read from structlog's contextvars -- the same
context the orchestrator binds ``trace_id`` into per tick and the order
manager binds ``order_key`` into per submission). ``envelope_of`` is a view
over an event, so producers keep publishing plain payloads and nothing on the
publish path changes (INV-032).

``correlated`` is how a producer adds ids to that context: a thin wrapper
over ``structlog.contextvars.bound_contextvars`` that refuses names outside
``CORRELATION_KEYS``, so a typo cannot silently start a second vocabulary.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import structlog

from src.eventbus.bus import CORRELATION_KEYS, Event

SCHEMA_VERSION = 1

_SEVERITIES = ("debug", "info", "warning", "error", "critical")


@contextmanager
def correlated(**ids: str) -> Iterator[None]:
    """Bind correlation ids for everything published inside the block."""
    unknown = sorted(set(ids) - set(CORRELATION_KEYS))
    if unknown:
        raise ValueError(f"not correlation keys: {', '.join(unknown)}")
    with structlog.contextvars.bound_contextvars(**ids):
        yield


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: str
    event_type: str
    timestamp_ms: int
    severity: str
    payload: Mapping[str, Any]
    trace_id: str | None = None
    causation_id: str | None = None
    decision_id: str | None = None
    order_id: str | None = None
    strategy_id: str | None = None
    model_id: str | None = None
    runtime_component_id: str | None = None
    change_id: str | None = None
    task_id: str | None = None
    component_id: str | None = None
    component_version: str | None = None
    actor: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp_ms": self.timestamp_ms,
            "severity": self.severity,
            "payload": dict(self.payload),
            "trace_id": self.trace_id,
            "causation_id": self.causation_id,
            "decision_id": self.decision_id,
            "order_id": self.order_id,
            "strategy_id": self.strategy_id,
            "model_id": self.model_id,
            "runtime_component_id": self.runtime_component_id,
            "change_id": self.change_id,
            "task_id": self.task_id,
            "component_id": self.component_id,
            "component_version": self.component_version,
            "actor": self.actor,
            "schema_version": self.schema_version,
        }


def _severity(event: Event) -> str:
    declared = event.data.get("severity")
    if isinstance(declared, str) and declared in _SEVERITIES:
        return declared
    if event.topic == "gate" and event.data.get("passed") is False:
        return "warning"
    return "info"


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def envelope_of(event: Event) -> EventEnvelope:
    """The universal envelope view of one published event."""
    ctx = event.context
    data = event.data
    kind = data.get("event_type") or data.get("kind")
    return EventEnvelope(
        event_id=event.event_id,
        event_type=f"{event.topic}.{kind}" if kind else event.topic,
        timestamp_ms=event.ts_ms,
        severity=_severity(event),
        payload=data,
        trace_id=_text(ctx.get("trace_id")),
        causation_id=_text(ctx.get("causation_id")),
        decision_id=_text(ctx.get("decision_id")),
        order_id=_text(ctx.get("order_key")),
        strategy_id=_text(ctx.get("strategy_id")),
        model_id=_text(ctx.get("model_id")),
        runtime_component_id=_text(ctx.get("runtime_component_id")),
        change_id=_text(ctx.get("change_id")),
        task_id=_text(ctx.get("task_id")),
        component_id=_text(data.get("component_id")),
        component_version=_text(data.get("component_version")),
        actor=_text(data.get("actor")),
    )
