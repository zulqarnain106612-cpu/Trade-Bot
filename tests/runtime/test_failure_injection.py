"""RES-019: every injected runtime failure ends in an explicit, safe state --
the component FAILED, QUARANTINED, untouched or rolled back, the change
REJECTED, FAILED, ROLLED_BACK or ROLLBACK_FAILED -- never a silent success.

Decides: RES-019"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from src.models.model_registry import ModelRegistry
from src.runtime.adapters import model_discoveries
from src.runtime.changes import ChangeManager, ChangeRequest, ChangeStatus
from src.runtime.contracts import (
    ComponentType,
    DependencyDescriptor,
    HealthReport,
    HealthState,
    LifecycleAction,
    LifecycleState,
)
from src.runtime.persistence import AuditPersister, DesiredStatePersister
from src.runtime.registry import RuntimeRegistry
from src.runtime.supervisor import ShadowModelController, SupervisorSet

from ._support import HUMAN, Clock, build_platform, spec, version

A = LifecycleAction
S = LifecycleState


@dataclass
class Case:
    name: str
    start: S
    action: A
    target: str | None = None
    fail_on: frozenset[A] | None = None
    unhealthy: bool = False
    deps: tuple[DependencyDescriptor, ...] = ()
    expected_version: str | None = None
    component_after: S | None = None
    change_after: ChangeStatus = ChangeStatus.FAILED
    version_after: str = "1"


CASES = [
    Case(
        "worker dies",
        S.STANDBY,
        A.ACTIVATE,
        fail_on=frozenset({A.ACTIVATE}),
        component_after=S.FAILED,
    ),
    Case(
        "model load fails",
        S.STOPPED,
        A.REPLACE,
        "2",
        fail_on=frozenset({A.REPLACE}),
        component_after=S.FAILED,
    ),
    Case(
        "config reload fails",
        S.ACTIVE,
        A.RELOAD,
        fail_on=frozenset({A.RELOAD}),
        component_after=S.FAILED,
    ),
    Case(
        "dependency missing",
        S.STOPPED,
        A.START,
        deps=(DependencyDescriptor("task:gone"),),
        component_after=S.STOPPED,
        change_after=ChangeStatus.REJECTED,
    ),
    Case(
        "stale version",
        S.ACTIVE,
        A.PAUSE,
        expected_version="0",
        component_after=S.ACTIVE,
        change_after=ChangeStatus.REJECTED,
    ),
    Case(
        "unhealthy after change",
        S.ACTIVE,
        A.REPLACE,
        "2",
        unhealthy=True,
        component_after=S.ACTIVE,
        change_after=ChangeStatus.ROLLED_BACK,
    ),
    Case(
        "rollback fails",
        S.ACTIVE,
        A.REPLACE,
        "2",
        unhealthy=True,
        fail_on=frozenset({A.ROLLBACK}),
        component_after=S.FAILED,
        change_after=ChangeStatus.ROLLBACK_FAILED,
        version_after="2",
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_injected_failures_end_in_explicit_states(case: Case) -> None:
    p = build_platform()
    p.registry.register(spec("w", deps=case.deps), observed_state=case.start)
    if case.fail_on is not None:
        p.controller.fail_with = RuntimeError(f"injected: {case.name}")
        p.controller.fail_on = case.fail_on
    if case.unhealthy:
        p.controller.health = HealthReport(HealthState.UNHEALTHY, "injected")
    change = p.changes.submit(
        ChangeRequest(
            "worker:w",
            case.action,
            HUMAN,
            case.name,
            target_version=None if case.target is None else version(case.target),
            expected_version=case.expected_version,
        )
    )
    if change.status is ChangeStatus.AWAITING_APPROVAL:
        p.changes.approve(change.change_id, HUMAN)
    if change.status is not ChangeStatus.REJECTED:
        change = p.changes.execute(change.change_id, HUMAN)
    record = p.registry.get("worker:w")
    assert change.status is case.change_after
    assert record.state is case.component_after
    assert record.version.version == case.version_after
    assert p.changes.audit_log(change.change_id), "every outcome is audited"


def test_partial_activation_leaves_the_live_model_in_place() -> None:
    """The gate passes, then the swap itself fails: nothing is half-promoted."""
    models = ModelRegistry(min_evaluations=1)
    models.set_live_model("m1")
    models.register_shadow("m2")
    models.record_shadow_prediction("m2", 0.9, 1)
    models.record_live_prediction_for_comparison("m2", 0.1, 1)

    def broken_promote(model_id: str) -> None:
        raise OSError("artifact store unreachable")

    models.promote_shadow = broken_promote  # type: ignore[method-assign]
    registry = RuntimeRegistry(clock=Clock())
    registry.register_all([(d.spec, d.state) for d in model_discoveries(models)])
    supervisors = SupervisorSet(registry, {ComponentType.MODEL: ShadowModelController(models)})
    changes = ChangeManager(registry, supervisors, clock=Clock())
    change = changes.submit(ChangeRequest("model:m2", A.ACTIVATE, HUMAN, "promote"))
    changes.approve(change.change_id, HUMAN)
    changes.record_stage(change.change_id, stage="shadow", passed=True, detail="ok", actor=HUMAN)
    failed = changes.execute(change.change_id, HUMAN)
    assert failed.status is ChangeStatus.FAILED
    assert models.live_model_id == "m1"
    assert registry.get("model:m1").state is S.ACTIVE
    assert registry.get("model:m2").state is S.FAILED


class _Down:
    async def upsert_runtime_desired_state(self, *args: Any) -> None:
        raise ConnectionError("database unavailable")

    async def insert_audit_event(self, *args: Any) -> None:
        raise ConnectionError("database unavailable")


async def test_database_and_event_delivery_failures_do_not_block_risk_off() -> None:
    p = build_platform()

    def dead_sink(entry: object) -> None:
        raise BrokenPipeError("event delivery failed")

    p.changes._sinks.append(dead_sink)
    audit = AuditPersister(_Down())
    p.changes._sinks.append(audit.enqueue)
    desired = DesiredStatePersister(_Down())
    desired.attach(p.registry)
    p.registry.register(spec("w"), observed_state=S.ACTIVE)
    change = p.changes.submit(ChangeRequest("worker:w", A.DRAIN, HUMAN, "outage"))
    done = p.changes.execute(change.change_id, HUMAN)
    assert done.status is ChangeStatus.PROMOTED
    assert p.registry.get("worker:w").state is S.DRAINED
    assert await desired.flush() == 0 and desired.pending == ("worker:w",)
    assert await audit.flush() == 0 and audit.pending == 3


def test_a_duplicate_command_is_one_change() -> None:
    p = build_platform()
    p.registry.register(spec("w"), observed_state=S.ACTIVE)
    request = ChangeRequest("worker:w", A.DRAIN, HUMAN, "dup", change_id="chg-dup")
    first = p.changes.submit(request)
    assert p.changes.submit(request) is first
    p.changes.execute(first.change_id, HUMAN)
    assert p.changes.submit(request).status is ChangeStatus.PROMOTED
    assert [c for c, *_ in p.controller.calls] == ["worker:w"]
