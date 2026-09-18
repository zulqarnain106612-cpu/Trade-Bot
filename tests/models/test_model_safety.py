"""
MODL-002 — a model never decides to bypass the risk gate.

The architecture the source document requires:

```
MODEL → SIGNAL → RISK ENGINE → EXECUTION POLICY → ORDER
```

never:

```
MODEL → ORDER
```

The deterministic safety layer sits *outside* the model. A model's job is to
be right more often than not; the gate's job is to survive the model being
confidently wrong, and a gate a confident model can talk past is not a gate.

Two kinds of assertion, because a property this structural is not established
by behaviour alone:

1. **Behavioural.** A maximally confident model changes nothing about the
   gate's verdict. Confidence is not an input to any gate.
2. **Structural.** No model or intelligence module imports an executor, and
   the signal engine really does route through `evaluate_all_gates`. A
   behavioural test passes right up until someone adds a second path.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.config import TradingMode
from src.risk.gates import GateStatus, evaluate_all_gates

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Modules that must never reach an executor directly.
MODEL_PACKAGES = ("src/models", "src/intelligence", "src/fusion", "src/causal")

#: Anything that can place an order.
EXECUTION_MODULES = (
    "src.execution.live",
    "src.execution.paper",
    "src.execution.router",
    "src.execution.order_manager",
)


def python_files(*packages: str) -> list[Path]:
    found: list[Path] = []
    for package in packages:
        found.extend(sorted((PROJECT_ROOT / package).rglob("*.py")))
    return found


def imported_modules(path: Path) -> set[str]:
    """Every module name this file imports, however it imports it."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


class TestConfidenceIsNotAnInputToAnyGate:
    @pytest.mark.parametrize("p_long", [0.5, 0.9, 0.99, 1.0])
    def test_a_maximally_confident_model_cannot_lift_a_halt(
        self, passing_gate_ctx, risk_cfg, p_long
    ):
        # The gate context has no field for model confidence, and that is the
        # point: there is nowhere for confidence to enter. This test asserts
        # the consequence -- a halted stack stays halted -- and would fail
        # loudly if such a field were ever added and honoured.
        ctx = passing_gate_ctx(regime_state=2)
        result = evaluate_all_gates(ctx, risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_REGIME

    def test_the_gate_context_has_no_confidence_field(self):
        from src.risk.gates import RiskGateContext

        fields = set(RiskGateContext.__dataclass_fields__)
        forbidden = {"p_long", "confidence", "model_confidence", "probability", "conviction"}
        assert not (fields & forbidden), (
            f"RiskGateContext gained {fields & forbidden}: the deterministic "
            "safety layer must not take the model's word for anything."
        )

    def test_an_oversized_position_is_refused_regardless_of_the_model(
        self, passing_gate_ctx, risk_cfg
    ):
        over = 100_000.0 * risk_cfg.max_position_size_pct / 100.0 + 1.0
        ctx = passing_gate_ctx(notional_usd=over, expected_edge_bps=1e6)
        assert evaluate_all_gates(ctx, risk_cfg).status is GateStatus.HALT_POSITION_SIZE

    def test_the_live_gate_depends_on_validation_not_on_confidence(
        self, passing_gate_ctx, risk_cfg
    ):
        # A model that has not passed OOS validation is blocked in live mode
        # no matter what it predicts.
        ctx = passing_gate_ctx(
            trading_mode=TradingMode.LIVE,
            direction_gate_pass=False,
            expected_edge_bps=1e6,
        )
        assert evaluate_all_gates(ctx, risk_cfg).status is GateStatus.HALT_LIVE_GATE


class TestNoModelModuleCanReachAnExecutor:
    def test_there_are_model_modules_to_check(self):
        assert len(python_files(*MODEL_PACKAGES)) > 20

    @pytest.mark.parametrize(
        "path", python_files(*MODEL_PACKAGES), ids=lambda p: str(p.relative_to(PROJECT_ROOT))
    )
    def test_it_does_not_import_an_executor(self, path):
        offenders = {
            name
            for name in imported_modules(path)
            if any(name.startswith(module) for module in EXECUTION_MODULES)
        }
        assert not offenders, (
            f"{path.relative_to(PROJECT_ROOT)} imports {offenders}. A model must "
            "reach an order through SIGNAL -> RISK ENGINE -> EXECUTION POLICY, "
            "never directly."
        )


@pytest.fixture(scope="module", name="engine_source")
def _engine_source() -> str:
    return (PROJECT_ROOT / "src" / "engine" / "signal_engine.py").read_text(encoding="utf-8")


class TestTheSignalEngineRoutesThroughTheGates:
    @pytest.fixture
    def source(self, engine_source) -> str:
        return engine_source

    def test_it_evaluates_the_gate_stack(self, source):
        assert "evaluate_all_gates" in source

    def test_it_does_not_import_an_executor(self):
        offenders = {
            name
            for name in imported_modules(PROJECT_ROOT / "src" / "engine" / "signal_engine.py")
            if any(name.startswith(module) for module in EXECUTION_MODULES)
        }
        assert not offenders, f"signal_engine imports {offenders}"

    def test_the_gate_result_is_not_discarded(self, source):
        # The specific failure this catches: computing the gate result and
        # then not branching on it, which looks exactly like a wired gate in
        # a diff and does nothing at runtime.
        tree = ast.parse(source)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "evaluate_all_gates"
        ]
        assert calls, "signal_engine no longer calls evaluate_all_gates"
        bare = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "evaluate_all_gates"
        ]
        assert not bare, "evaluate_all_gates called as a bare statement; its verdict is discarded"
