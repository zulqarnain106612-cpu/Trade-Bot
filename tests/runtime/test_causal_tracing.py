"""RES-015: events carry the correlation context bound where they were
published; a decision is traceable end to end and a rejected one names its
first blocking condition; runtime changes are on the bus and in the
hash-chained audit trail."""

from __future__ import annotations

import pytest
import structlog

from src.diagnostics.audit_trail import AuditTrail
from src.diagnostics.decision_trace import DecisionTraceIndex
from src.eventbus import CORRELATION_KEYS, Event, EventBus, correlated, envelope_of
from src.runtime.changes import ChangeManager, ChangeRequest
from src.runtime.contracts import ComponentType, LifecycleAction, LifecycleState
from src.runtime.events import audit_trail_sink, eventbus_audit_sink, transition_publisher
from src.runtime.registry import RuntimeRegistry
from src.runtime.supervisor import SupervisorSet

from ._support import HUMAN, NOBODY, Clock, FakeController, spec

S = LifecycleState


@pytest.fixture(autouse=True)
def _clean_context() -> None:
    structlog.contextvars.clear_contextvars()


def test_publish_copies_only_correlation_keys() -> None:
    bus = EventBus()
    sub = bus.subscribe(["signal"])
    with structlog.contextvars.bound_contextvars(trace_id="t1", secret="not-copied"):
        bus.publish("signal", {"tradeable": True})
    bus.publish("signal", {"tradeable": True})
    first, second = list(sub._queue)
    assert first.context == {"trace_id": "t1"}
    assert second.context == {} and first.event_id != second.event_id
    assert "secret" not in CORRELATION_KEYS


def test_correlated_refuses_unknown_names() -> None:
    with pytest.raises(ValueError, match="not correlation keys: orderid"):
        with correlated(orderid="x"):
            pass  # pragma: no cover - the context manager refuses before entering


def test_envelope_fields_severity_and_type() -> None:
    event = Event(
        "gate",
        {"passed": False, "status": "drawdown", "kind": "block", "actor": "risk"},
        5,
        context={"trace_id": "t", "order_key": "k1", "change_id": "c"},
    )
    env = envelope_of(event)
    assert (env.event_type, env.severity, env.timestamp_ms) == ("gate.block", "warning", 5)
    assert (env.trace_id, env.order_id, env.change_id, env.actor) == ("t", "k1", "c", "risk")
    assert env.to_dict()["schema_version"] == 1
    declared = envelope_of(Event("health", {"severity": "critical"}, 1))
    assert declared.severity == "critical" and declared.trace_id is None
    bogus = envelope_of(Event("health", {"severity": "loud"}, 1))
    assert (bogus.severity, bogus.event_type) == ("info", "health")
    typed = envelope_of(Event("runtime", {"event_type": "transition", "component_id": "w"}, 1))
    assert (typed.event_type, typed.component_id) == ("runtime.transition", "w")


async def test_a_trade_is_traced_end_to_end_and_rejections_name_the_first_block() -> None:
    bus = EventBus()
    index = DecisionTraceIndex()
    sub = bus.subscribe(["signal", "gate", "order", "position", "price", "health"])
    with correlated(trace_id="filled"):
        bus.publish("price", {"mid": 1.0})
        bus.publish("signal", {"tradeable": True, "direction": 1})
        bus.publish("gate", {"passed": True, "status": "vol", "size_scalar": 0.5})
        with correlated(order_key="ord-1"):
            bus.publish("order", {"state": "FILLED"})
            bus.publish("position", {"side": "long"})
    with correlated(trace_id="rejected"):
        bus.publish("signal", {"tradeable": True})
        bus.publish("gate", {"passed": False, "status": "drawdown", "reason": "dd 9%"})
    with correlated(trace_id="skipped"):
        bus.publish("signal", {"tradeable": False, "skip_reason": "low confidence"})
    with correlated(trace_id="ordered"):
        bus.publish("order", {"state": "OPEN"})
    with correlated(trace_id="partial"):
        bus.publish("price", {"mid": 1.0})
        bus.publish("health", {"ok": True})  # off the decision path
    bus.publish("signal", {"tradeable": True})  # no trace bound
    sub.close()
    await index.consume(sub)

    filled = index.trace("filled")
    assert filled is not None
    assert filled.stages == ("market_data", "signal", "risk", "order", "fill")
    assert filled.final_decision == "FILLED" and filled.first_blocking is None
    assert filled.events[-1].order_id == "ord-1"
    rejected = index.trace("rejected")
    assert rejected is not None and rejected.final_decision == "REJECTED"
    assert rejected.first_blocking == {
        "stage": "risk",
        "gate": "drawdown",
        "reason": "dd 9%",
        "event_id": rejected.events[1].event_id,
    }
    out = rejected.to_dict()
    assert out["request"]["payload"] == {"tradeable": True}
    assert [r["payload"]["status"] for r in out["risk_results"]] == ["drawdown"]
    skipped = index.trace("skipped")
    assert skipped is not None and skipped.final_decision == "NO_TRADE"
    assert skipped.first_blocking is not None
    assert skipped.first_blocking["reason"] == "low confidence"
    assert index.trace("ordered").final_decision == "ORDERED"  # type: ignore[union-attr]
    partial = index.trace("partial")
    assert partial is not None and partial.final_decision == "INCOMPLETE"
    assert partial.to_dict()["request"] is None
    assert index.trace("nope") is None
    assert [t.trace_id for t in index.recent(2)] == ["partial", "ordered"]


def test_the_index_is_bounded_on_both_axes() -> None:
    with pytest.raises(ValueError):
        DecisionTraceIndex(max_traces=0)
    index = DecisionTraceIndex(max_traces=2, max_events=1)
    for trace_id in ("a", "b", "c"):
        assert index.record(Event("signal", {}, 1, context={"trace_id": trace_id}))
    assert index.trace("a") is None and index.trace("c") is not None
    assert not index.record(Event("gate", {}, 2, context={"trace_id": "c"}))
    assert index.truncated == 1


def test_runtime_changes_reach_the_bus_and_the_hash_chained_trail() -> None:
    bus = EventBus()
    sub = bus.subscribe(["runtime"])
    trail = AuditTrail()
    registry = RuntimeRegistry(clock=Clock())
    registry.add_transition_listener(transition_publisher(bus))
    supervisors = SupervisorSet(registry, {ComponentType.WORKER: FakeController()})
    changes = ChangeManager(
        registry,
        supervisors,
        clock=Clock(),
        audit_sinks=[eventbus_audit_sink(bus), audit_trail_sink(trail)],
    )
    registry.register(spec("w"), observed_state=S.ACTIVE)
    change = changes.submit(ChangeRequest("worker:w", LifecycleAction.DRAIN, HUMAN, "drain"))
    changes.execute(change.change_id, HUMAN)
    changes.submit(ChangeRequest("worker:w", LifecycleAction.DRAIN, NOBODY, "x"))
    events = list(sub._queue)
    kinds = [(e.data["kind"], e.data.get("event") or e.data.get("action")) for e in events]
    assert kinds[:4] == [
        ("transition", "DISCOVER"),
        ("change", "submitted:APPROVED"),
        ("transition", "DRAIN"),
        ("change", "executed"),
    ]
    drain = events[2]
    assert drain.context == {"runtime_component_id": "worker:w", "change_id": change.change_id}
    assert events[0].context == {"runtime_component_id": "worker:w"}
    rejected = events[-1]
    assert rejected.data["severity"] == "warning"
    entries = trail.entries()
    assert [e.reason_code for e in entries] == [
        "submitted:APPROVED",
        "executed",
        "promoted",
        "submitted:REJECTED",
    ]
    assert entries[1].prev_hash == entries[0].entry_hash
    assert trail.verify_chain_integrity() == (True, None)
