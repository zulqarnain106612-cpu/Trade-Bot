"""RES-019: the runtime platform has no path around the trading safety
mechanisms. Read from the source, so a future edit that adds one fails here
rather than in production:

* src/runtime imports nothing from execution, nothing that sets the
  execution mode, and no risk gate;
* the only kill-switch use is reading it (is_registered / is_enabled /
  disabled_reason) -- never re_enable or a manual toggle;
* no model enters the live slot except through ShadowModelController, which
  calls evaluate_shadow before promote_shadow; set_live_model is never called;
* tuning parameters are never written (update_current), and no strategy is
  registered or unregistered;
* nothing in src/runtime or the runtime API executes or patches code;
* the API's runtime handlers reach the platform only through the change
  manager, the reconciler and read-only registry calls -- never a
  supervisor or a controller.

Decides: RES-019"""

from __future__ import annotations

import ast
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
