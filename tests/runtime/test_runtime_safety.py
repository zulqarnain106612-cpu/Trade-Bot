"""RES-019: the runtime platform has no path around the trading safety
mechanisms. Read from the source, so a future edit that adds one fails here
rather than in production:

* src/runtime imports nothing from execution, nothing that sets the
  execution mode, and no risk gate;
* the kill switch is read (is_registered / is_enabled / disabled_reason)
  and pulled only one way: ``disable``, by the QUARANTINE controller in
  controllers.py -- a risk reduction. Nothing in the runtime re-enables a
  strategy; only the gauntlet does;
* the subsystem levers the production controllers pull (``disable``,
  ``discard_shadow_now``, ``set_paused_now``) are called from controllers.py
  and nowhere else in the runtime or its API;
* no model enters the live slot except through ShadowModelController, which
  calls evaluate_shadow before promote_shadow; set_live_model is never called;
* tuning parameters are never written (update_current), and no strategy is
  registered or unregistered;
* nothing in src/runtime or the runtime API executes or patches code;
* the API's runtime handlers reach the platform only through the change
  manager, the reconciler and read-only registry calls -- never a
  supervisor or a controller.

Decides: RES-019, RES-022"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = sorted((ROOT / "src" / "runtime").glob("*.py"))
API_RUNTIME = ROOT / "src" / "api" / "runtime_control.py"


def _trees() -> dict[str, ast.Module]:
    return {p.name: ast.parse(p.read_text(encoding="utf-8")) for p in [*RUNTIME, API_RUNTIME]}


TREES = _trees()


def _imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
    return found


def _attribute_calls(tree: ast.Module) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_the_files_under_test_exist() -> None:
    assert len(RUNTIME) >= 10 and API_RUNTIME.exists()


@pytest.mark.parametrize("name", sorted(TREES))
def test_no_import_reaches_execution_or_risk_gates(name: str) -> None:
    forbidden = ("src.execution", "src.risk.gates", "src.risk.position_sizer")
    bad = {m for m in _imports(TREES[name]) if m.startswith(forbidden)}
    assert not bad, f"{name} imports {sorted(bad)}"


@pytest.mark.parametrize("name", sorted(TREES))
def test_no_safety_lever_is_pulled(name: str) -> None:
    calls = _attribute_calls(TREES[name])
    forbidden = {
        "re_enable",  # kill switch: only the gauntlet re-enables a strategy
        "set_live_model",  # a model enters the live slot only by promotion
        "update_current",  # a parameter moves only through the tuning runner
        "set_execution_mode",
        "set_risk_controls",
        "register_strategy",
        "unregister",
    }
    assert not calls & forbidden, f"{name} calls {sorted(calls & forbidden)}"


@pytest.mark.parametrize("name", sorted(TREES))
def test_no_code_is_executed_or_patched(name: str) -> None:
    for node in ast.walk(TREES[name]):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"exec", "eval", "compile", "__import__", "setattr"}
    assert "importlib" not in _imports(TREES[name])


CONTROLLER_LEVERS = {"disable", "discard_shadow_now", "set_paused_now"}


def test_subsystem_levers_are_pulled_only_by_the_controllers() -> None:
    callers = {
        name: sorted(_attribute_calls(tree) & CONTROLLER_LEVERS) for name, tree in TREES.items()
    }
    assert {n: c for n, c in callers.items() if c} == {"controllers.py": sorted(CONTROLLER_LEVERS)}


def test_promotion_is_gated_by_the_model_registrys_evaluation() -> None:
    callers = [n for n, t in TREES.items() if "promote_shadow" in _attribute_calls(t)]
    assert callers == ["supervisor.py"]
    cls = next(
        n
        for n in ast.walk(TREES["supervisor.py"])
        if isinstance(n, ast.ClassDef) and n.name == "ShadowModelController"
    )
    source = ast.unparse(cls)
    assert source.index("evaluate_shadow") < source.index("promote_shadow")
    assert "PromotionRefusedError" in source


def test_the_api_reaches_runtime_only_through_the_change_manager() -> None:
    tree = TREES["runtime_control.py"]
    chains = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "platform"
        ):
            chains.add(f"{node.value.attr}.{node.attr}")
    allowed_prefixes = ("changes.", "reconciler.reconcile_once", "traces.trace")
    allowed_registry = {"registry.find", "registry.dependents", "registry.history"}
    stray = {c for c in chains if not c.startswith(allowed_prefixes) and c not in allowed_registry}
    assert not stray, f"runtime_control reaches {sorted(stray)}"
    assert not any(c.startswith(("supervisors.", "registry.apply")) for c in chains)


def test_change_manager_owns_horizon_shadow_activation_and_promotion() -> None:
    from dataclasses import replace

    from src.runtime.changes import ChangeRequest, ChangeStatus, RuntimeLifecycleUnavailableError
    from src.runtime.contracts import ComponentType, LifecycleAction, LifecycleState
    from ._support import HUMAN, REQUESTER, SYSTEM, spec, version, build_platform

    class HorizonLifecycle:
        def __init__(self):
            self.staged: list[str] = []
            self.completed: list[str] = []

        def record_shadow_stage(self, component):
            self.staged.append(component.component_id)
            return '{"eligible": true, "evaluation_id": "paired-evidence"}'

        def complete_promotion(self, component):
            self.completed.append(component.component_id)

    platform = build_platform()
    component_spec = replace(
        spec("horizon", ComponentType.MODEL),
        version=version("1", horizon_id=0),
    )
    platform.registry.register(component_spec, observed_state=LifecycleState.STANDBY)
    lifecycle = HorizonLifecycle()
    platform.changes._horizon_models = lifecycle
    change = platform.changes.submit(
        ChangeRequest(
            component_id="model:horizon",
            action=LifecycleAction.ACTIVATE,
            actor=REQUESTER,
            reason="promote evaluated horizon",
        )
    )

    executed = platform.changes.decide_lifecycle(
        change.change_id, "approve", HUMAN, system_actor=SYSTEM
    )
    assert executed.status is ChangeStatus.EXECUTED
    assert lifecycle.staged == ["model:horizon"]
    assert platform.changes.audit_log(change.change_id)

    promoted = platform.changes.decide_lifecycle(
        change.change_id, "promote", HUMAN, system_actor=SYSTEM
    )
    assert promoted.status is ChangeStatus.PROMOTED
    assert lifecycle.completed == ["model:horizon"]


def test_change_manager_refuses_horizon_approval_without_lifecycle_service() -> None:
    from src.runtime.changes import (
        ChangeError,
        ChangeRequest,
        RuntimeLifecycleUnavailableError,
    )
    from src.runtime.contracts import ComponentType, LifecycleAction, LifecycleState
    from ._support import HUMAN, REQUESTER, SYSTEM, spec, version, build_platform

    platform = build_platform()
    component_spec = replace(
        spec("horizon", ComponentType.MODEL),
        version=version("1", horizon_id=0),
    )
    platform.registry.register(component_spec, observed_state=LifecycleState.STANDBY)
    change = platform.changes.submit(
        ChangeRequest(
            component_id="model:horizon",
            action=LifecycleAction.ACTIVATE,
            actor=REQUESTER,
            reason="promote evaluated horizon",
        )
    )
    with pytest.raises(RuntimeLifecycleUnavailableError, match="lifecycle is unavailable"):
        platform.changes.decide_lifecycle(
            change.change_id, "approve", HUMAN, system_actor=SYSTEM
        )
    # The refusal happens before approval is committed, so retry cannot find a
    # half-approved change with no available evaluator.
    assert platform.changes.get(change.change_id).status.value == "AWAITING_APPROVAL"

    class HorizonLifecycle:
        def record_shadow_stage(self, component):
            return "fresh paired evidence"

        def complete_promotion(self, component):
            return None

    platform.changes._horizon_models = HorizonLifecycle()
    executed = platform.changes.decide_lifecycle(
        change.change_id, "approve", HUMAN, system_actor=SYSTEM
    )
    assert executed.status.value == "EXECUTED"
    platform.changes._horizon_models = None
    with pytest.raises(RuntimeLifecycleUnavailableError, match="lifecycle is unavailable"):
        platform.changes.decide_lifecycle(
            change.change_id, "promote", HUMAN, system_actor=SYSTEM
        )
    assert platform.changes.get(change.change_id).status.value == "EXECUTED"

    platform.changes._horizon_models = HorizonLifecycle()

    def missing_component(_component_id):
        return None

    platform.changes._registry.find = missing_component
    with pytest.raises(ChangeError, match="unknown component"):
        platform.changes.decide_lifecycle(
            change.change_id, "approve", HUMAN, system_actor=SYSTEM
        )
    assert platform.changes.get(change.change_id).status.value == "EXECUTED"


def test_change_manager_routes_simple_decisions_and_refuses_unknown_verbs() -> None:
    from src.runtime.changes import ChangeError, ChangeRequest, ChangeStatus
    from src.runtime.contracts import LifecycleAction, LifecycleState
    from ._support import HUMAN, REQUESTER, SYSTEM, build_platform, spec

    platform = build_platform()
    platform.registry.register(spec("worker"), observed_state=LifecycleState.STOPPED)
    change = platform.changes.submit(
        ChangeRequest(
            component_id="worker:worker",
            action=LifecycleAction.START,
            actor=REQUESTER,
            reason="start worker under supervision",
        )
    )
    executed = platform.changes.decide_lifecycle(
        change.change_id, "approve", HUMAN, system_actor=SYSTEM
    )
    assert executed.status is ChangeStatus.EXECUTED
    promoted = platform.changes.decide_lifecycle(
        change.change_id, "promote", HUMAN, system_actor=SYSTEM
    )
    assert promoted.status is ChangeStatus.PROMOTED

    second = platform.changes.submit(
        ChangeRequest(
            component_id="worker:worker",
            action=LifecycleAction.DRAIN,
            actor=REQUESTER,
            reason="drain worker",
        )
    )
    cancelled = platform.changes.decide_lifecycle(
        second.change_id, "cancel", REQUESTER, system_actor=SYSTEM
    )
    assert cancelled.status is ChangeStatus.CANCELLED
    with pytest.raises(ChangeError, match="unknown decision"):
        platform.changes.decide_lifecycle(
            second.change_id, "delete", HUMAN, system_actor=SYSTEM
        )



def test_change_manager_routes_execute_rollback_and_non_horizon_model_stage() -> None:
    from src.runtime.changes import ChangeRequest, ChangeStatus
    from src.runtime.contracts import ComponentType, LifecycleAction, LifecycleState
    from ._support import HUMAN, REQUESTER, SYSTEM, build_platform, spec

    platform = build_platform()
    platform.registry.register(spec("drainable"), observed_state=LifecycleState.STANDBY)
    drain = platform.changes.submit(
        ChangeRequest(
            component_id="worker:drainable",
            action=LifecycleAction.DRAIN,
            actor=REQUESTER,
            reason="reduce worker exposure",
        )
    )
    drained = platform.changes.decide_lifecycle(
        drain.change_id, "execute", REQUESTER, system_actor=SYSTEM
    )
    assert drained.status is ChangeStatus.PROMOTED

    platform.registry.register(spec("restartable"), observed_state=LifecycleState.STOPPED)
    start = platform.changes.submit(
        ChangeRequest(
            component_id="worker:restartable",
            action=LifecycleAction.START,
            actor=REQUESTER,
            reason="start under supervision",
        )
    )
    executed = platform.changes.decide_lifecycle(
        start.change_id, "approve", HUMAN, system_actor=SYSTEM
    )
    assert executed.status is ChangeStatus.EXECUTED
    rolled_back = platform.changes.decide_lifecycle(
        start.change_id, "rollback", REQUESTER, reason="operator rollback", system_actor=SYSTEM
    )
    assert rolled_back.status is ChangeStatus.ROLLED_BACK

    model_spec = spec("external-shadow", ComponentType.MODEL)
    platform.registry.register(model_spec, observed_state=LifecycleState.STANDBY)
    model_change = platform.changes.submit(
        ChangeRequest(
            component_id="model:external-shadow",
            action=LifecycleAction.ACTIVATE,
            actor=REQUESTER,
            reason="model has an externally managed shadow stage",
        )
    )
    approved = platform.changes.decide_lifecycle(
        model_change.change_id, "approve", HUMAN, system_actor=SYSTEM
    )
    assert approved.status is ChangeStatus.APPROVED
