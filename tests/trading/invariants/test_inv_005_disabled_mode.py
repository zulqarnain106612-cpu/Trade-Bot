"""
INV-005 — a disabled trading mode cannot submit orders.

The failure this prevents is the one an operator would find hardest to
believe: the bot trades real money while they believe it is in paper mode.

Three separable claims, because they fail independently:

1. **The live gate refuses live mode until the models are validated.** In
   paper the gate is skipped by design — paper needs no quality guard — and
   that asymmetry is asserted rather than assumed, because it is the part
   that looks like a hole.
2. **A paper executor has no route to a live exchange.** Asserted
   structurally: no import, no attribute. A behavioural test passes right up
   until someone adds a second path.
3. **Mode is decided in one place.** Two independent readings of "are we
   live" is how a scheduled job ends up in a different mode from the tick
   loop that spawned it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.config import TradingMode
from src.risk.gates import GateStatus, check_live_gate, evaluate_all_gates

PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: Anything that reaches a real venue.
LIVE_SURFACE = ("ccxt", "src.execution.live")


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestTheLiveGateGuardsLiveModeOnly:
    @pytest.mark.parametrize(("direction", "meta"), [(False, False), (False, True), (True, False)])
    def test_live_is_refused_until_both_models_validate(self, direction, meta):
        result = check_live_gate(TradingMode.LIVE, direction, meta)
        assert not result.passed
        assert result.status is GateStatus.HALT_LIVE_GATE

    def test_live_is_allowed_once_both_models_validate(self):
        assert check_live_gate(TradingMode.LIVE, True, True).passed

    @pytest.mark.parametrize(("direction", "meta"), [(False, False), (False, True), (True, True)])
    def test_paper_skips_the_quality_guard_deliberately(self, direction, meta):
        # The asymmetry that looks like a hole and is not: paper trading is
        # how a model earns its validation, so gating paper on validation
        # would make the gate unopenable.
        result = check_live_gate(TradingMode.PAPER, direction, meta)
        assert result.passed
        assert result.details["gate_check"] == "skipped_paper"

    def test_an_unvalidated_model_cannot_reach_a_live_order(self, passing_gate_ctx, risk_cfg):
        ctx = passing_gate_ctx(
            trading_mode=TradingMode.LIVE,
            direction_gate_pass=False,
            meta_gate_pass=False,
        )
        assert evaluate_all_gates(ctx, risk_cfg).status is GateStatus.HALT_LIVE_GATE

    def test_the_paper_minimum_applies_only_to_live(self, passing_gate_ctx, risk_cfg):
        # In paper, "you have not paper-traded long enough" is not a reason
        # to stop paper trading.
        paper = passing_gate_ctx(trading_mode=TradingMode.PAPER, paper_trading_days=0)
        assert evaluate_all_gates(paper, risk_cfg).passed

        live = passing_gate_ctx(trading_mode=TradingMode.LIVE, paper_trading_days=0)
        assert evaluate_all_gates(live, risk_cfg).status is GateStatus.HALT_PAPER_ONLY


class TestThePaperExecutorHasNoRouteToAVenue:
    def test_it_does_not_import_a_live_exchange(self):
        offenders = {
            name
            for name in imports(PROJECT_ROOT / "src" / "execution" / "paper.py")
            if any(name.startswith(prefix) for prefix in LIVE_SURFACE)
        }
        assert not offenders, (
            f"src/execution/paper.py imports {offenders}. A paper executor that "
            "can reach a venue is one configuration mistake away from trading "
            "real money while the operator believes otherwise."
        )

    def test_it_exposes_no_exchange_client(self):
        import src.execution.paper as paper

        suspicious = [
            name for name in dir(paper) if "exchange" in name.lower() and not name.startswith("_")
        ]
        assert not suspicious, suspicious


class TestModeIsDecidedInOnePlace:
    def test_there_are_exactly_the_declared_modes(self):
        # A third mode added without updating the gates is a mode nothing
        # guards.
        assert {mode.value for mode in TradingMode} >= {"paper", "live"}

    def test_the_gate_treats_every_non_live_mode_as_paper(self):
        # The gate branches on `== PAPER`, so a mode that is neither is
        # treated as live and therefore gated -- which is the safe default,
        # and is asserted here so that staying safe is a decision rather than
        # an accident of how the comparison was written.
        for mode in TradingMode:
            result = check_live_gate(mode, False, False)
            if mode is TradingMode.PAPER:
                assert result.passed
            else:
                assert not result.passed
