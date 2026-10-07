"""GOV-068: supervisors share one infrastructure, preview before acting, refuse
unsupported transitions without calling the controller, and the model
supervisor promotes only through the model registry's own evaluation."""

from __future__ import annotations

import threading

import pytest

from src.models.model_registry import ModelRegistry
from src.runtime.adapters import model_discoveries
from src.runtime.contracts import (
    ComponentRecord,
    ComponentType,
    ComponentVersion,
    HealthState,
    InvalidTransitionError,
    LifecycleAction,
    LifecycleState,
    RuntimeContractError,
    UnsupportedActionError,
)
from src.runtime.registry import RuntimeRegistry
from src.runtime.supervisor import (
    SUPERVISOR_TYPES,
    EngineSupervisor,
    ModelSupervisor,
    ObserveOnlyController,
    PromotionRefusedError,
    ShadowModelController,
    Supervisor,
    SupervisorSet,
    TransitionInProgressError,
    WrongSupervisorError,
)

from ._support import COLD, Clock, FakeController, Platform, spec, version

A = LifecycleAction
S = LifecycleState


def test_one_supervisor_class_per_component_type() -> None:
    assert set(SUPERVISOR_TYPES) == set(ComponentType)
    for ctype, cls in SUPERVISOR_TYPES.items():
        assert issubclass(cls, Supervisor) and cls.component_type is ctype


def test_a_supervisor_needs_a_type() -> None:
    with pytest.raises(ValueError, match="component type"):
        Supervisor(RuntimeRegistry(), ObserveOnlyController())
    generic = Supervisor(
        RuntimeRegistry(), ObserveOnlyController(), component_type=ComponentType.TASK
    )
    assert generic.supervised_type is ComponentType.TASK


def test_execute_calls_the_controller_then_records(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.STANDBY)
    t = platform.supervisors.for_component("worker:w").execute(
        "worker:w", A.ACTIVATE, actor="op", change_id="c1"
    )
    assert platform.controller.calls == [("worker:w", A.ACTIVATE, None)]
    assert (t.to_state, t.change_id) == (S.ACTIVE, "c1")
    assert platform.registry.get("worker:w").state is S.ACTIVE


def test_replace_and_rollback_hand_the_resolved_version_to_the_controller(
    platform: Platform,
) -> None:
    platform.registry.register(spec("w"), observed_state=S.ACTIVE)
    sup = platform.supervisors.for_type(ComponentType.WORKER)
    sup.execute("worker:w", A.REPLACE, actor="op", target_version=version("2"))
    sup.execute("worker:w", A.ROLLBACK, actor="op")
    assert platform.controller.calls == [
        ("worker:w", A.REPLACE, "2"),
        ("worker:w", A.ROLLBACK, "1"),
    ]
    assert platform.registry.get("worker:w").version.version == "1"


@pytest.mark.parametrize(
    ("caps_state", "action", "error"),
    [
        (S.ACTIVE, A.STOP, InvalidTransitionError),
        (S.ACTIVE, A.START, InvalidTransitionError),
    ],
)
def test_illegal_requests_never_reach_the_controller(
    platform: Platform, caps_state: S, action: A, error: type[Exception]
) -> None:
    platform.registry.register(spec("w"), observed_state=caps_state)
    with pytest.raises(error):
        platform.supervisors.for_component("worker:w").execute("worker:w", action, actor="op")
    assert platform.controller.calls == []


def test_unsupported_actions_never_reach_the_controller(platform: Platform) -> None:
    platform.registry.register(spec("e", ComponentType.ENGINE, caps=COLD), observed_state=S.ACTIVE)
    with pytest.raises(UnsupportedActionError):
        platform.supervisors.for_component("engine:e").execute(
            "engine:e", A.REPLACE, actor="op", target_version=version("2")
        )
    with pytest.raises(RuntimeContractError):
        platform.supervisors.for_component("engine:e").execute(
            "engine:e", A.DEACTIVATE, actor="op", target_version=version("2")
        )
    assert platform.controller.calls == []


def test_a_supervisor_refuses_other_types(platform: Platform) -> None:
    platform.registry.register(spec("w"), observed_state=S.STANDBY)
    engines = platform.supervisors.for_type(ComponentType.ENGINE)
    assert isinstance(engines, EngineSupervisor)
    with pytest.raises(WrongSupervisorError):
        engines.execute("worker:w", A.ACTIVATE, actor="op")


def test_observe_only_controller_performs_nothing() -> None:
    registry = RuntimeRegistry(clock=Clock())
    record = registry.register(spec("e", ComponentType.ENGINE))
    controller = ObserveOnlyController()
    with pytest.raises(UnsupportedActionError, match="observe-only"):
        controller.perform(record, A.START, None)
    assert controller.probe(record) == record.health


def test_concurrent_actions_on_one_component_are_refused_not_interleaved() -> None:
    entered, release = threading.Event(), threading.Event()

    class Slow(FakeController):
        def perform(
            self,
            record: ComponentRecord,
            action: LifecycleAction,
            target_version: ComponentVersion | None,
        ) -> None:
            entered.set()
            release.wait(timeout=5)
            super().perform(record, action, target_version)

    registry = RuntimeRegistry(clock=Clock())
    registry.register(spec("w"), observed_state=S.STANDBY)
    supervisors = SupervisorSet(registry, {ComponentType.WORKER: Slow()})
    sup = supervisors.for_type(ComponentType.WORKER)
    worker = threading.Thread(
        target=sup.execute, args=("worker:w", A.ACTIVATE), kwargs={"actor": "a"}
    )
    worker.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(TransitionInProgressError):
            sup.execute("worker:w", A.ACTIVATE, actor="b")
    finally:
        release.set()
        worker.join()
    assert registry.get("worker:w").state is S.ACTIVE


Models = tuple[ModelRegistry, RuntimeRegistry, SupervisorSet]


@pytest.fixture
def models() -> Models:
    models = ModelRegistry(min_evaluations=2)
    models.set_live_model("m1")
    models.register_shadow("m2")
    models.register_shadow("m3")
    registry = RuntimeRegistry(clock=Clock())
    registry.register_all([(d.spec, d.state) for d in model_discoveries(models)])
    supervisors = SupervisorSet(registry, {ComponentType.MODEL: ShadowModelController(models)})
    return models, registry, supervisors


def test_promotion_refused_by_the_model_gate_leaves_the_shadow_untouched(models: Models) -> None:
    model_registry, registry, supervisors = models
    with pytest.raises(PromotionRefusedError, match="insufficient evaluations"):
        supervisors.for_component("model:m2").execute("model:m2", A.ACTIVATE, actor="op")
    record = registry.get("model:m2")
    assert (record.state, record.failure_count) == (S.STANDBY, 0)
    assert model_registry.live_model_id == "m1"


def test_promotion_through_the_gate_retires_the_old_live_model(models: Models) -> None:
    model_registry, registry, supervisors = models
    for _ in range(2):
        model_registry.record_shadow_prediction("m2", 0.9, 1)
        model_registry.record_live_prediction_for_comparison("m2", 0.2, 1)
    sup = supervisors.for_component("model:m2")
    assert isinstance(sup, ModelSupervisor)
    sup.execute("model:m2", A.ACTIVATE, actor="op")
    assert model_registry.live_model_id == "m2"
    assert registry.get("model:m2").state is S.ACTIVE
    old = registry.get("model:m1")
    assert old.state is S.STOPPED and "superseded by model:m2" in old.health.detail
    assert registry.get("model:m3").state is S.STANDBY
    assert sup.probe("model:m2").detail == "live"


def test_discarding_a_shadow_and_probing_models(models: Models) -> None:
    model_registry, registry, supervisors = models
    sup = supervisors.for_component("model:m2")
    assert sup.probe("model:m2").detail == "shadow"
    sup.execute("model:m2", A.STOP, actor="op")
    assert model_registry.shadow_ids() == ["m3"]
    assert registry.get("model:m2").state is S.STOPPED
    assert sup.probe("model:m2").state is HealthState.UNHEALTHY
    # Only the declared actions exist for a shadow.
    record = registry.get("model:m2")
    with pytest.raises(UnsupportedActionError):
        ShadowModelController(model_registry).perform(record, A.PAUSE, None)


def test_a_non_activate_success_has_no_side_effects_on_other_models(models: Models) -> None:
    _, registry, supervisors = models
    supervisors.for_component("model:m2").execute("model:m2", A.STOP, actor="op")
    assert registry.get("model:m1").state is S.ACTIVE
