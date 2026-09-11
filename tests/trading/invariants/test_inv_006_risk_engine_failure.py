"""
INV-006 — Risk engine failure cannot result in an executable order.

The hazard is not that the risk engine can fail. It is that a failure can be
mistaken for a pass. Three ways that happens, and one assertion each:

  1. A gate that catches its own exception and returns "fine". The drift gate
     is the one gate here that runs an external check, and it fails *closed*
     on an exception -- deliberately unlike the intelligence gates, which fail
     open because a third-party feed being down says nothing about our model.
  2. A gate that raises and a caller that swallows it. `evaluate_all_gates`
     does not catch, so the exception reaches the caller; what is asserted
     here is that it does not become a pass along the way.
  3. A gate stack that is never reached. Covered by asserting the stack blocks
     from each failing component independently rather than only in
     combination.

The source document's rule for this class: *"A swallowed exception in a
trading system can become a financial-security vulnerability."*
"""

from __future__ import annotations

import pytest

from src.config import TradingMode
from src.risk.gates import (
    GateStatus,
    check_performance_drift,
    evaluate_all_gates,
)


class _Drift:
    """Minimal stand-in for PerformanceDriftDetector."""

    def __init__(self, *, raises: Exception | None = None, drifted: bool = False) -> None:
        self._raises = raises
        self._drifted = drifted

    def check_drift(self):
        if self._raises is not None:
            raise self._raises
        return _DriftResult(self._drifted)

    def get_live_metrics(self):
        return {
            "total_live_trades": 600,
            "rolling_sharpe": 1.8,
            "rolling_winrate": 0.55,
            "rolling_accuracy": 0.58,
            "max_live_drawdown_pct": 4.0,
        }


class _DriftResult:
    def __init__(self, drifted: bool) -> None:
        self.drifted = drifted
        self.reason = "sharpe fell below baseline"
        self.metric = "rolling_sharpe"
        self.live_value = 0.2
        self.baseline_value = 1.8
        self.drift_pp = -1.6


class TestAFailingCheckIsNotAPass:
    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("db down"),
            ValueError("bad baseline"),
            KeyError("metric"),
            TimeoutError("slow"),
            ZeroDivisionError("no trades"),
        ],
        ids=["runtime", "value", "key", "timeout", "zero-division"],
    )
    def test_drift_gate_fails_closed_on_any_exception(self, error):
        result = check_performance_drift(_Drift(raises=error))
        assert not result.passed
        assert result.status is GateStatus.HALT_DRIFT
        assert "failed" in result.reason

    def test_drift_gate_blocks_when_drift_is_detected(self):
        result = check_performance_drift(_Drift(drifted=True))
        assert not result.passed
        assert result.status is GateStatus.HALT_DRIFT

    def test_a_healthy_detector_passes(self):
        assert check_performance_drift(_Drift(drifted=False)).passed

    def test_no_detector_passes_because_the_gate_is_not_wired(self):
        # Distinct from "the check ran and said fine". Asserted so that the
        # day a detector is always supplied, this test is the thing that has
        # to be deleted -- deliberately, in a diff.
        result = check_performance_drift(None)
        assert result.passed
        assert result.details["reason"] == "drift_detector_not_enabled"


class TestTheStackDoesNotTurnAFailureIntoAPass:
    def test_a_raising_detector_blocks_the_live_stack(self, passing_gate_ctx, risk_cfg):
        ctx = passing_gate_ctx(
            trading_mode=TradingMode.LIVE,
            drift_detector=_Drift(raises=RuntimeError("db down")),
        )
        result = evaluate_all_gates(ctx, risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_DRIFT

    def test_the_drift_gate_is_live_only(self, passing_gate_ctx, risk_cfg):
        # Halting the paper track on drift would stop the very run that is
        # gathering the evidence about whether the drift persists.
        ctx = passing_gate_ctx(
            trading_mode=TradingMode.PAPER,
            drift_detector=_Drift(raises=RuntimeError("db down")),
        )
        assert evaluate_all_gates(ctx, risk_cfg).passed

    @pytest.mark.parametrize(
        "gate",
        [
            "check_capital_preservation_floor",
            "check_slippage_veto",
            "check_daily_drawdown",
            "check_consecutive_losses",
            "check_regime_gate",
            "check_position_size",
            "check_live_gate",
            "check_exchange_stress",
            "check_whale_activity",
        ],
    )
    def test_a_gate_that_raises_propagates_rather_than_passing(
        self, monkeypatch, passing_gate_ctx, risk_cfg, gate
    ):
        # The stack has no try/except, which is the correct design: an
        # exception reaching the executor is unmistakable, whereas a caught
        # one becomes a judgement call made by whoever wrote the handler.
        import src.risk.gates as gates_module

        def _boom(*_args, **_kwargs):
            raise RuntimeError(f"{gate} exploded")

        monkeypatch.setattr(gates_module, gate, _boom)
        with pytest.raises(RuntimeError, match="exploded"):
            evaluate_all_gates(passing_gate_ctx(), risk_cfg)


class TestNoGateModuleSwallowsAnExceptionIntoAPass:
    def test_every_except_block_in_the_risk_package_is_narrow(self):
        # A `except:` or `except Exception: pass` inside a risk module is the
        # exact shape the source document calls a financial-security
        # vulnerability. The repository-wide version of this check lands in
        # PR-006 (GOV-004); this is the risk package's own copy, because
        # INV-006 depends on it.
        import ast
        from pathlib import Path

        package = Path(__file__).resolve().parents[3] / "src" / "risk"
        offenders: list[str] = []
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                bare = node.type is None
                only_pass = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
                if bare or only_pass:
                    offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, f"silently-swallowed exceptions in src/risk: {offenders}"
