"""
The README's risk-gate list is what an operator trusts, so pin it.

Before this test the list was wrong in four ways at once: it omitted the
capital-preservation floor (the OUTERMOST gate) and the performance-drift
gate, listed portfolio correlation as a gate when it is a sizing scalar
that never halts, and gave an order that did not match evaluation order --
which matters because the stack short-circuits, so the order decides which
gate gets reported.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


def _stack_order() -> list[str]:
    """The check_* calls inside evaluate_all_gates, in source order."""
    source = Path("src/risk/gates.py").read_text(encoding="utf-8")
    tree = ast.parse(source, "src/risk/gates.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate_all_gates":
            names = [
                n.func.id
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id.startswith("check_")
            ]
            # ast.walk is not source-ordered; recover order from the segment.
            segment = ast.get_source_segment(source, node) or ""
            return sorted(set(names), key=segment.index)
    raise AssertionError("evaluate_all_gates not found")


def _readme_rows() -> list[str]:
    rows = []
    in_table = False
    for line in Path("README.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## Risk Gates"):
            in_table = True
            continue
        if in_table and line.startswith("**Not a gate:**"):
            break
        m = re.match(r"\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|", line)
        if in_table and m:
            rows.append(m.group(2))
    return rows


def test_the_readme_lists_every_gate_in_evaluation_order() -> None:
    stack = _stack_order()
    rows = _readme_rows()
    assert len(rows) == len(stack), (
        f"README lists {len(rows)} gates, the stack evaluates {len(stack)}: {stack}"
    )


def test_each_row_names_its_gate() -> None:
    """Row N must plausibly describe the Nth gate, not merely be present."""
    keywords = {
        "check_capital_preservation_floor": "capital preservation",
        "check_slippage_veto": "slippage",
        "check_daily_drawdown": "daily drawdown",
        "check_consecutive_losses": "consecutive loss",
        "check_regime_gate": "regime",
        "check_position_size": "position size",
        "check_paper_minimum_days": "paper minimum",
        "check_live_gate": "live gate",
        "check_performance_drift": "performance drift",
        "check_exchange_stress": "exchange stress",
        "check_whale_activity": "whale",
    }
    for fn, row in zip(_stack_order(), _readme_rows(), strict=True):
        assert keywords[fn] in row.lower(), f"README row {row!r} does not describe {fn}"


def test_correlation_is_not_claimed_to_be_a_gate() -> None:
    """It produces a sizing scalar; calling it a gate overstates the control."""
    readme = Path("README.md").read_text(encoding="utf-8")
    section = readme.split("## Risk Gates")[1].split("## ")[0]
    assert "**Not a gate:**" in section
