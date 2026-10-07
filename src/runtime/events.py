"""
Runtime observability: the platform's transitions and change audit, on the
event bus and in the hash-chained audit trail.

* ``transition_publisher`` -- a registry transition listener publishing
  every lifecycle transition on the ``runtime`` topic.
* ``eventbus_audit_sink`` -- a change-manager audit sink publishing every
  change step on the ``runtime`` topic, with ``change_id`` and
  ``runtime_component_id`` bound so the envelope carries them.
* ``audit_trail_sink`` -- the same steps in ``diagnostics.AuditTrail``, whose
  hash chain makes the history tamper-evident.

Publishing is the bus's never-raise, never-block ``publish`` (INV-032); the
registry and the change manager also guard each listener and sink, so an
observability failure cannot undo or block a runtime change.
"""

from __future__ import annotations

from collections.abc import Callable

from src.diagnostics.audit_trail import AuditTrail
from src.eventbus import EventBus, correlated, get_event_bus
from src.runtime.changes import AuditEntry
from src.runtime.contracts import TransitionRecord


def transition_publisher(bus: EventBus | None = None) -> Callable[[TransitionRecord], None]:
    """Publishes on ``bus``, or on the process bus (``get_event_bus``)."""
    target = bus if bus is not None else get_event_bus()

    def publish(transition: TransitionRecord) -> None:
        ids = {"runtime_component_id": transition.component_id}
        if transition.change_id is not None:
            ids["change_id"] = transition.change_id
        with correlated(**ids):
            target.publish("runtime", {"kind": "transition", **transition.to_dict()})

    return publish


def eventbus_audit_sink(bus: EventBus | None = None) -> Callable[[AuditEntry], None]:
    """Publishes on ``bus``, or on the process bus (``get_event_bus``)."""
    target = bus if bus is not None else get_event_bus()

    def publish(entry: AuditEntry) -> None:
        with correlated(change_id=entry.change_id, runtime_component_id=entry.component_id):
            target.publish(
                "runtime",
                {
                    "kind": "change",
                    "severity": "warning" if _is_negative(entry.event) else "info",
                    **entry.to_dict(),
                },
            )

    return publish


def audit_trail_sink(trail: AuditTrail) -> Callable[[AuditEntry], None]:
    def record(entry: AuditEntry) -> None:
        trail.record(
            event_type="runtime_change",
            reason_code=entry.event,
            details={
                "change_id": entry.change_id,
                "component_id": entry.component_id,
                "actor": entry.actor,
                "detail": entry.detail,
            },
            ts_ms=int(entry.at.timestamp() * 1000),
        )

    return record


_NEGATIVE = ("refused", "failed", "rejected", "unavailable", "stale", "rolled_back")


def _is_negative(event: str) -> bool:
    lowered = event.lower()
    return any(word in lowered for word in _NEGATIVE)
