"""GOV-070: an executed change rolls back through its inverse action, restores
the previous desired state, and says so when no inverse exists."""

from __future__ import annotations

import pytest

from src.runtime.changes import ChangeError, ChangeRequest, ChangeStatus
from src.runtime.contracts import (
    CapabilitySet,
    HealthReport,
    HealthState,
    LifecycleAction,
    LifecycleState,
)

from ._support import HUMAN, SYSTEM, Platform, spec, version

A = LifecycleAction
S = LifecycleState


def _executed_replace(platform: Platform) -> str:
    platform.registry.register(spec("w"), observed_state=S.ACTIVE)
    change = platform.changes.submit(
        ChangeRequest("worker:w", A.REPLACE, HUMAN, "upgrade", target_version=version("2"))
    )
    platform.changes.approve(change.change_id, HUMAN)
    assert platform.changes.execute(change.change_id, HUMAN).status is ChangeStatus.EXECUTED
    return change.change_id


def test_manual_rollback_restores_version_and_desired_state(platform: Platform) -> None:
    change_id = _executed_replace(platform)
    record = platform.registry.get("worker:w")
    assert record.version.version == "2" and record.desired is not None
    rolled = platform.changes.rollback(change_id, SYSTEM, "watchdog: drawdown")
    assert rolled.status is ChangeStatus.ROLLED_BACK
    assert rolled.rollback == "watchdog: drawdown; ROLLBACK applied"
    record = platform.registry.get("worker:w")
    assert record.version.version == "1" and record.desired is None
    with pytest.raises(ChangeError, match="not EXECUTED"):
        platform.changes.rollback(change_id, SYSTEM, "again")


def test_unhealthy_after_execution_rolls_back_automatically(platform: Platform) -> None:
    platform.controller.health = HealthReport(HealthState.UNHEALTHY, "latency 9s")
    platform.registry.register(spec("w"), observed_state=S.ACTIVE)
    change = platform.changes.submit(
        ChangeRequest("worker:w", A.REPLACE, HUMAN, "upgrade", target_version=version("2"))
    )
    platform.changes.approve(change.change_id, HUMAN)
    result = platform.changes.execute(change.change_id, HUMAN)
    assert result.status is ChangeStatus.ROLLED_BACK
    assert "unhealthy after change: latency 9s" in (result.rollback or "")
    assert platform.registry.get("worker:w").version.version == "1"


def test_promotion_of_an_unhealthy_change_rolls_it_back(platform: Platform) -> None:
    change_id = _executed_replace(platform)
    platform.controller.health = HealthReport(HealthState.UNHEALTHY, "errors")
    assert platform.changes.promote(change_id, HUMAN).status is ChangeStatus.ROLLED_BACK


def test_no_supported_inverse_is_reported(platform: Platform) -> None:
    caps = CapabilitySet(frozenset({A.START}))
    platform.registry.register(spec("w", caps=caps), observed_state=S.STOPPED)
    change = platform.changes.submit(ChangeRequest("worker:w", A.START, HUMAN, "x"))
    platform.changes.approve(change.change_id, HUMAN)
    platform.changes.execute(change.change_id, HUMAN)
    result = platform.changes.rollback(change.change_id, HUMAN, "manual")
    assert result.status is ChangeStatus.ROLLBACK_FAILED
    assert result.rollback == "manual; no supported inverse of START"


def test_a_failing_inverse_is_reported(platform: Platform) -> None:
    change_id = _executed_replace(platform)
    platform.controller.fail_with = RuntimeError("cannot restore")
    result = platform.changes.rollback(change_id, HUMAN, "manual")
    assert result.status is ChangeStatus.ROLLBACK_FAILED
    assert "ROLLBACK failed" in (result.rollback or "")
    assert platform.registry.get("worker:w").state is S.FAILED
