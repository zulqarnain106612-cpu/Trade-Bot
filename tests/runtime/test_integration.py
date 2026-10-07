"""RES-020: the whole runtime path holds together. One component goes from
discovery, through an operator's desired state, the reconciler, the change
manager, a supervisor and its controller, onto the event bus and into the
hash-chained audit trail and storage, and back out through the API -- and a
trading decision published on the same bus is traceable through the same API.

Decides: RES-020"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from src.diagnostics.audit_trail import AuditTrail
from src.eventbus import EventBus, correlated
from src.runtime.adapters import Discovery
from src.runtime.contracts import ComponentType, DependencyDescriptor, LifecycleState
from src.runtime.platform import TRACE_TOPICS, build_runtime_platform

from ._support import FakeController, spec

S = LifecycleState
SECRET = "s" * 32
OP = {"operator": "alice", "operator_secret": SECRET}
KEY = {"x-api-key": "t" * 32}


class _Storage:
    def __init__(self) -> None:
        self.desired: dict[str, Any] = {}
        self.audit: list[dict[str, Any]] = []

    async def upsert_runtime_desired_state(
        self, component_id: str, desired: dict[str, Any] | None, updated_ms: int
    ) -> None:
        self.desired[component_id] = desired

    async def fetch_runtime_desired_states(self) -> dict[str, Any]:
        return {k: v for k, v in self.desired.items() if v is not None}

    async def insert_audit_event(
        self, event_type: str, operator: str, details: dict[str, Any] | None = None
    ) -> None:
        self.audit.append(details or {})


async def test_desired_state_to_audit_and_trace_through_the_api() -> None:
    from src.api.main import AppState, app

    bus = EventBus()
    trail = AuditTrail()
    storage = _Storage()
    platform = build_runtime_platform(
        bus=bus,
        discoveries=[
            Discovery(spec("feed", ComponentType.TASK), S.ACTIVE),
            Discovery(spec("w", deps=(DependencyDescriptor("task:feed"),)), S.ACTIVE),
        ],
        trail=trail,
        controllers={t: FakeController() for t in ComponentType},
        desired_backend=storage,
        audit_backend=storage,
    )
    runtime_events = bus.subscribe(["runtime"])
    trace_events = bus.subscribe(TRACE_TOPICS)
    state = AppState()
    state.ready = True
    state.storage = AsyncMock()
    state.orchestrator = MagicMock()
    state.runtime = platform
    env = {"API_SECRET_KEY": "t" * 32, "OPERATOR_SECRET": SECRET}
    with patch.dict(os.environ, env), patch("src.api.main._state", state):
        client = TestClient(app, raise_server_exceptions=False)

        impact = client.get("/runtime/dependencies", headers=KEY).json()
        assert impact["order"] == ["task:feed", "worker:w"]

        body = {**OP, "component_id": "worker:w", "target_state": "drained", "reason": "maint"}
        assert client.post("/runtime/desired", headers=KEY, json=body).status_code == 200
        assert client.get("/runtime", headers=KEY).json()["mismatches"][0]["plan"] == ["DRAIN"]

        (outcome,) = client.post("/runtime/reconcile", headers=KEY, json=OP).json()
        assert outcome["outcome"] == "executed: PROMOTED"
        change_id = outcome["change_id"]
        assert client.get("/runtime", headers=KEY).json()["mismatches"] == []
        change = client.get(f"/runtime/changes/{change_id}", headers=KEY).json()
        assert change["actor"]["name"] == "reconciler" and change["classification"] == "LIVE_SAFE"

        # A tick on the same bus: signal, then a refusing risk gate.
        with correlated(trace_id="tick-1"):
            bus.publish("signal", {"tradeable": True, "direction": 1})
            bus.publish("gate", {"passed": False, "status": "drawdown", "reason": "dd 9%"})
        trace_events.close()
        await platform.traces.consume(trace_events)
        trace = client.get("/runtime/traces/tick-1", headers=KEY).json()
        assert trace["final_decision"] == "REJECTED"
        assert trace["first_blocking"]["gate"] == "drawdown"

    # The change is on the bus with its correlation ids ...
    published = list(runtime_events._queue)
    runtime_events.close()
    drain = next(e for e in published if e.data.get("action") == "DRAIN")
    assert drain.context == {"runtime_component_id": "worker:w", "change_id": change_id}
    # ... in the hash-chained trail ...
    assert trail.verify_chain_integrity() == (True, None)
    assert {e.details["change_id"] for e in trail.entries()} >= {change_id}
    # ... and in storage, desired state and the audit, flushed by the API calls.
    assert storage.desired["worker:w"]["target_state"] == "DRAINED"
    assert [a["event"] for a in storage.audit if a["change_id"] == change_id] == [
        "submitted:APPROVED",
        "executed",
        "promoted",
    ]
