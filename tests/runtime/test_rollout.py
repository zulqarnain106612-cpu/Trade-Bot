"""RES-013: the rollout class follows the policy order, and shadow/canary
classes execute only after a passed stage.

Decides: RES-013"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.runtime.changes import ChangeError, ChangeRequest, ChangeStatus
from src.runtime.classification import classify
from src.runtime.contracts import (
    CapabilitySet,
    ChangeClass,
    ComponentRecord,
    ComponentType,
    LifecycleAction,
    LifecycleState,
)

from ._support import ALL_ACTIONS, COLD, HOT, HUMAN, SYSTEM, Platform, spec

A = LifecycleAction
S = LifecycleState
C = ChangeClass
NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _record(ctype: ComponentType, state: S, caps: CapabilitySet = HOT) -> ComponentRecord:
    return ComponentRecord(spec("x", ctype, caps=caps), state, NOW, NOW)


@pytest.mark.parametrize(
    ("ctype", "state", "caps", "action", "expected"),
    [
        (ComponentType.WORKER, S.ACTIVE, COLD, A.REPLACE, C.DRAIN_REQUIRED),
        (
            ComponentType.WORKER,
            S.ACTIVE,
            CapabilitySet(frozenset({A.REPLACE, A.STOP})),
            A.REPLACE,
            C.RESTART_REQUIRED,
        ),
        (
            ComponentType.WORKER,
            S.ACTIVE,
            CapabilitySet(frozenset({A.ROLLBACK})),
            A.ROLLBACK,
            C.FORBIDDEN,
        ),
        (ComponentType.WORKER, S.ACTIVE, HOT, A.START, C.FORBIDDEN),
        (ComponentType.STRATEGY, S.ACTIVE, HOT, A.QUARANTINE, C.LIVE_SAFE),
        (ComponentType.WORKER, S.DISCOVERED, HOT, A.VALIDATE, C.LIVE_SAFE),
        (ComponentType.MODEL, S.STANDBY, HOT, A.ACTIVATE, C.SHADOW_REQUIRED),
        (ComponentType.STRATEGY, S.STANDBY, HOT, A.ACTIVATE, C.CANARY_REQUIRED),
        (ComponentType.ENGINE, S.STANDBY, HOT, A.ACTIVATE, C.CANARY_REQUIRED),
        (ComponentType.TUNING, S.ACTIVE, HOT, A.REPLACE, C.CANARY_REQUIRED),
        (ComponentType.TUNING, S.STOPPED, HOT, A.REPLACE, C.LIVE_GATED),
        (ComponentType.WORKER, S.ACTIVE, HOT, A.REPLACE, C.LIVE_GATED),
        (ComponentType.WORKER, S.STANDBY, HOT, A.ACTIVATE, C.LIVE_GATED),
        (ComponentType.WORKER, S.PAUSED, HOT, A.RESUME, C.LIVE_GATED),
        (ComponentType.WORKER, S.DRAINED, COLD, A.ROLLBACK, C.LIVE_GATED),
    ],
)
def test_classification_policy(
    ctype: ComponentType, state: S, caps: CapabilitySet, action: A, expected: C
) -> None:
    rollout, reasons = classify(_record(ctype, state, caps), action)
    assert rollout is expected and reasons


def test_dependency_issues_forbid_into_service_but_not_risk_off() -> None:
    record = _record(ComponentType.WORKER, S.STANDBY)
    assert classify(record, A.ACTIVATE, ["dep down"]) == (C.FORBIDDEN, ("dep down",))
    assert classify(record, A.DRAIN, ["dep down"])[0] is C.LIVE_SAFE


def test_starting_a_stopped_component_is_gated() -> None:
    caps = CapabilitySet(ALL_ACTIONS)
    assert classify(_record(ComponentType.WORKER, S.STOPPED, caps), A.START)[0] is C.LIVE_GATED


def _shadow_change(platform: Platform) -> str:
    platform.registry.register(spec("m", ComponentType.MODEL), observed_state=S.STANDBY)
    change = platform.changes.submit(ChangeRequest("model:m", A.ACTIVATE, HUMAN, "promote"))
    assert change.classification is C.SHADOW_REQUIRED
    platform.changes.approve(change.change_id, HUMAN)
    return change.change_id


def test_shadow_required_change_waits_for_a_passed_shadow(platform: Platform) -> None:
    change_id = _shadow_change(platform)
    with pytest.raises(ChangeError, match="a passed shadow stage"):
        platform.changes.execute(change_id, HUMAN)
    with pytest.raises(ChangeError, match="needs no canary stage"):
        platform.changes.record_stage(
            change_id, stage="canary", passed=True, detail="", actor=SYSTEM
        )
    staged = platform.changes.record_stage(
        change_id, stage="shadow", passed=True, detail="beats live 0.61>0.55", actor=SYSTEM
    )
    assert staged.status is ChangeStatus.STAGED
    assert staged.to_dict()["stages"] == [
        {"stage": "shadow", "passed": True, "detail": "beats live 0.61>0.55", "by": "watchdog"}
    ]
    assert platform.changes.execute(change_id, HUMAN).status is ChangeStatus.EXECUTED


def test_a_failed_stage_fails_the_change(platform: Platform) -> None:
    change_id = _shadow_change(platform)
    failed = platform.changes.record_stage(
        change_id, stage="shadow", passed=False, detail="worse than live", actor=HUMAN
    )
    assert failed.status is ChangeStatus.FAILED
    assert failed.result == "shadow stage failed: worse than live"
    assert platform.controller.calls == []


def test_a_live_gated_change_has_no_stage(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.STOPPED)
    change = platform.changes.submit(ChangeRequest("worker:w", A.START, HUMAN, "x"))
    platform.changes.approve(change.change_id, HUMAN)
    with pytest.raises(ChangeError, match="needs no shadow stage"):
        platform.changes.record_stage(
            change.change_id, stage="shadow", passed=True, detail="", actor=HUMAN
        )
