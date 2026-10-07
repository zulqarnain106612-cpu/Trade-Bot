"""GOV-069: impact analysis names the affected components, the validations
and rollout the change needs, and the failure domains it can reach."""

from __future__ import annotations

import pytest

from src.runtime.contracts import (
    ChangeClass,
    ComponentType,
    DependencyDescriptor,
    LifecycleAction,
    LifecycleState,
)
from src.runtime.dependencies import DependencyGraph
from src.runtime.registry import RuntimeRegistry

from ._support import Clock, spec, version

A = LifecycleAction
S = LifecycleState
D = DependencyDescriptor


@pytest.fixture
def registry() -> RuntimeRegistry:
    r = RuntimeRegistry(clock=Clock())
    r.register(spec("features", ComponentType.TASK, v="12"), observed_state=S.ACTIVE)
    r.register(
        spec("BTC", ComponentType.MODEL, v="31", deps=(D("task:features", "12"),)),
        observed_state=S.ACTIVE,
    )
    r.register(
        spec("trend", ComponentType.STRATEGY, deps=(D("model:BTC"),)), observed_state=S.ACTIVE
    )
    return r


def test_feature_set_change_reaches_the_decision_path(registry: RuntimeRegistry) -> None:
    report = DependencyGraph.from_registry(registry).impact(
        "task:features", A.REPLACE, version("13")
    )
    assert report.affected == ("model:BTC", "strategy:trend")
    assert report.issues == ("model:BTC pins task:features at 12; replacing with 13 breaks it",)
    assert report.rollout_mode is ChangeClass.FORBIDDEN
    assert report.failure_domains == ("model", "strategy", "task", "decision-path")
    assert report.required_validations == (
        "backtest",
        "health probe",
        "risk-gate regression",
        "shadow evaluation",
    )
    out = report.to_dict()
    assert out["rollout_mode"] == "FORBIDDEN" and out["action"] == "REPLACE"


def test_a_compatible_replace_needs_a_canary_on_the_decision_path(
    registry: RuntimeRegistry,
) -> None:
    report = DependencyGraph.from_registry(registry).impact("model:BTC", A.REPLACE, version("32"))
    assert report.issues == ()
    assert report.rollout_mode is ChangeClass.CANARY_REQUIRED
    assert report.affected == ("strategy:trend",)


def test_into_service_needs_running_dependencies(registry: RuntimeRegistry) -> None:
    registry.register(
        spec("w", deps=(D("worker:idle"), D("task:features"))), observed_state=S.STANDBY
    )
    registry.register(spec("idle"), observed_state=S.STOPPED)
    report = DependencyGraph.from_registry(registry).impact("worker:w", A.ACTIVATE)
    assert report.issues == ("worker:w needs worker:idle running; it is STOPPED",)
    assert report.rollout_mode is ChangeClass.FORBIDDEN


def test_taking_risk_off_is_never_blocked_by_dependencies(registry: RuntimeRegistry) -> None:
    report = DependencyGraph.from_registry(registry).impact("task:features", A.DRAIN)
    assert report.rollout_mode is ChangeClass.LIVE_SAFE
    assert report.affected == ("model:BTC", "strategy:trend")


def test_replace_without_a_version_still_reports(registry: RuntimeRegistry) -> None:
    report = DependencyGraph.from_registry(registry).impact("task:features", A.REPLACE)
    assert report.issues == ()


def test_unknown_component(registry: RuntimeRegistry) -> None:
    with pytest.raises(KeyError):
        DependencyGraph.from_registry(registry).impact("task:nope", A.STOP)
