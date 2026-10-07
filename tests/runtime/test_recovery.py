"""RES-019: after a restart the platform rebuilds from discovery, restores the
operator's desired state, keeps the change audit that was written, and the
reconciler takes risk off again without waiting for a person.

Decides: RES-019"""

from __future__ import annotations

from typing import Any

from src.eventbus import EventBus
from src.runtime.adapters import Discovery
from src.runtime.changes import ChangeRequest
from src.runtime.contracts import ComponentType, LifecycleAction, LifecycleState
from src.runtime.persistence import AuditPersister
from src.runtime.platform import build_runtime_platform

from ._support import HUMAN, FakeController, spec

S = LifecycleState


class _Storage:
    """The two storage calls the platform makes, in memory."""

    def __init__(self) -> None:
        self.desired: dict[str, dict[str, Any]] = {}
        self.audit: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_audit = False

    async def upsert_runtime_desired_state(
        self, component_id: str, desired: dict[str, Any] | None, updated_ms: int
    ) -> None:
        if desired is None:
            self.desired.pop(component_id, None)
        else:
            self.desired[component_id] = desired

    async def fetch_runtime_desired_states(self) -> dict[str, dict[str, Any]]:
        return dict(self.desired)

    async def insert_audit_event(
        self, event_type: str, operator: str, details: dict[str, Any] | None = None
    ) -> None:
        if self.fail_audit:
            raise ConnectionError("down")
        self.audit.append((event_type, operator, details or {}))


def _boot(storage: _Storage, state: S) -> Any:
    return build_runtime_platform(
        bus=EventBus(),
        discoveries=[Discovery(spec("w"), state)],
        controllers={t: FakeController() for t in ComponentType},
        desired_backend=storage,
        audit_backend=storage,
    )


async def test_desired_state_and_audit_survive_a_restart_and_risk_off_resumes() -> None:
    storage = _Storage()
    first = _boot(storage, S.ACTIVE)
    change = first.changes.submit(
        ChangeRequest("worker:w", LifecycleAction.DRAIN, HUMAN, "maintenance")
    )
    first.changes.execute(change.change_id, HUMAN)
    assert await first.flush() == 4  # one desired state, three audit entries
    assert [d["event"] for _, _, d in storage.audit] == [
        "submitted:APPROVED",
        "executed",
        "promoted",
    ]
    assert {e for e, _, _ in storage.audit} == {"runtime_change"}

    # The process restarts; discovery finds the worker running again.
    second = _boot(storage, S.ACTIVE)
    assert await second.restore() == []
    record = second.registry.get("worker:w")
    assert record.desired is not None and record.desired.target_state is S.DRAINED
    (outcome,) = second.reconciler.reconcile_once()
    assert outcome.outcome == "executed: PROMOTED"
    assert second.registry.get("worker:w").state is S.DRAINED


async def test_the_audit_is_written_in_order_and_resumes_after_an_outage() -> None:
    storage = _Storage()
    persister = AuditPersister(storage)
    platform = _boot(storage, S.ACTIVE)
    platform.changes._sinks.append(persister.enqueue)
    change = platform.changes.submit(
        ChangeRequest("worker:w", LifecycleAction.DRAIN, HUMAN, "drain")
    )
    storage.fail_audit = True
    platform.changes.execute(change.change_id, HUMAN)
    assert await persister.flush() == 0 and persister.pending == 3
    storage.fail_audit = False
    assert await persister.flush() == 3 and persister.pending == 0


async def test_a_platform_without_backends_flushes_nothing() -> None:
    platform = build_runtime_platform(bus=EventBus(), discoveries=[])
    assert await platform.flush() == 0
    assert await platform.restore() == []
    assert platform.summary()["components"] == 0
