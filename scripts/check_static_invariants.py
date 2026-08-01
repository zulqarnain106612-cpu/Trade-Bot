#!/usr/bin/env python3
"""
Structural invariants that ruff and mypy do not check.

Every check here was written because it found a real defect in this
repository, not because it seemed prudent. They are AST-only — nothing is
imported and nothing is executed — so this is safe to run anywhere and takes
under a second.

The recurring shape they catch is *connectivity*: something is declared,
tested, and documented, but nothing on the other side consumes it — or two
sides consume each other by position while disagreeing about meaning.

Usage:
    python3 scripts/check_static_invariants.py

Exit codes:
    0  — all invariants hold
    1  — one or more violations (details printed to stdout)

Integration:
    Add alongside the existing coverage-floor gate in ci.yml:
        python3 scripts/check_static_invariants.py
"""

from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

# Sites where a positional slice is the deliberate legacy fallback, guarded by
# a name-based path taken first. Removing the fallback would break model
# artifacts written before feature_columns was recorded.
_POSITIONAL_SLICE_ALLOWED = {
    ("src/models/trainer.py", "predict_direction"),
    ("src/models/trainer.py", "predict_meta"),
    ("src/tuning/backtest_harness.py", "_predict_direction_batch"),
}


def _py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), str(path))


def _enclosing_function(tree: ast.Module) -> dict[ast.AST, str]:
    """Map every node to the name of the function that contains it."""
    owner: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner.setdefault(child, node.name)
    return owner


def check_import_cycles() -> list[str]:
    """
    Module-level import cycles fail at import time, and no test can run to
    report it — the collection error is all you get.
    """
    modules: dict[str, Path] = {}
    for path in _py_files(SRC):
        name = _rel(path)[:-3].replace("/", ".")
        modules[name.removesuffix(".__init__")] = path

    graph: dict[str, set[str]] = {}
    for name, path in modules.items():
        edges: set[str] = set()
        for node in _parse(path).body:  # top level only; a deferred import cannot cycle
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src"):
                edges.add(node.module)
            elif isinstance(node, ast.Import):
                edges.update(a.name for a in node.names if a.name.startswith("src"))
        graph[name] = {e for e in edges if e in modules}

    white, grey, black = 0, 1, 2
    colour: dict[str, int] = defaultdict(int)
    stack: list[str] = []
    cycles: list[str] = []

    def visit(node: str) -> None:
        colour[node] = grey
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if colour[nxt] == grey:
                cycles.append(" -> ".join([*stack[stack.index(nxt) :], nxt]))
            elif colour[nxt] == white:
                visit(nxt)
        stack.pop()
        colour[node] = black

    for node in sorted(graph):
        if colour[node] == white:
            visit(node)
    return [f"import cycle: {c}" for c in cycles]


def check_positional_column_slices() -> list[str]:
    """
    ``frame.columns[:n]`` / ``series.index[:n]`` reconciles by POSITION.

    That is safe only while both sides build their column list the same way.
    It shipped here as a model-inference path where training selected columns
    by coverage and inference selected them by finiteness — so a mismatch put
    one feature's value in another feature's slot, with no shape error to
    reveal it because the counts still agreed.
    """
    problems = []
    for path in _py_files(SRC):
        tree = _parse(path)
        owner = _enclosing_function(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)):
                continue
            if node.value.attr not in ("index", "columns") or not isinstance(node.slice, ast.Slice):
                continue
            key = (_rel(path), owner.get(node, "<module>"))
            if key not in _POSITIONAL_SLICE_ALLOWED:
                problems.append(
                    f"{_rel(path)}:{node.lineno} positional slice of .{node.value.attr} "
                    f"in {key[1]}() — select by name, or add to the allowlist with a reason"
                )
    return problems


def check_zip_is_strict() -> list[str]:
    """A non-strict zip silently truncates to the shorter side."""
    problems: list[str] = []
    for path in _py_files(SRC):
        problems.extend(
            f"{_rel(path)}:{node.lineno} zip() without strict="
            for node in ast.walk(_parse(path))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "zip"
            and not any(k.arg == "strict" for k in node.keywords)
        )
    return problems


def check_no_orphan_tasks() -> list[str]:
    """A discarded create_task swallows its exception until garbage collection."""
    problems: list[str] = []
    for path in _py_files(SRC):
        tree = _parse(path)
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        problems.extend(
            f"{_rel(path)}:{node.lineno} create_task() result discarded — "
            "keep a reference and attach a done-callback"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_task"
            and isinstance(parents.get(node), ast.Expr)
        )
    return problems


def check_settings_are_read() -> list[str]:
    """
    A setting nothing reads is a promise the software does not keep.

    Two of these were load-bearing when first scanned: half of a self-tuning
    cadence guard, and a bar-retention window whose pruner had no caller.
    """
    known_decorative = {
        "log_as_json",
        "rate_limit_per_minute",  # ccxt's own enableRateLimit governs this
        "rate_limit_per_second",
    }
    config = REPO / "src" / "config.py"
    declared: dict[str, str] = {}
    for cls in _parse(config).body:
        if not (isinstance(cls, ast.ClassDef) and cls.name.endswith("Settings")):
            continue
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                name = stmt.target.id
                if not name.startswith("_") and name != "model_config":
                    declared[name] = cls.name

    used: set[str] = set()
    for root in ("src", "tests", "scripts"):
        for path in _py_files(REPO / root):
            tree = _parse(path)
            # A field's own `name: type = Field(...)` line is a declaration,
            # not a use. Counting it made this check pass unconditionally --
            # every setting appeared "used" by the line that declared it.
            declaration_targets = {
                node.target
                for node in ast.walk(tree)
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
            }
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    used.add(node.attr)
                elif isinstance(node, ast.Name) and node not in declaration_targets:
                    used.add(node.id)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    used.add(node.value)

    return [
        f"src/config.py {cls}.{name} is declared but nothing reads it"
        for name, cls in sorted(declared.items())
        if name not in used and name not in known_decorative
    ]


def check_every_gate_status_is_reachable() -> list[str]:
    """
    A declared halt the gate stack can never emit is a risk control that
    does not exist. HALT_DRIFT was exactly that: produced only by a function
    absent from evaluate_all_gates().
    """
    gates = REPO / "src" / "risk" / "gates.py"
    tree = _parse(gates)
    members = [
        stmt.targets[0].id
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "GateStatus"
        for stmt in cls.body
        if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name)
    ]
    stack_source = ""
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "evaluate_all_gates"
        ):
            stack_source = ast.dump(node)

    # Which check_* functions does the stack actually call?
    called = set(re.findall(r"id='(check_\w+)'", stack_source))
    produced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in called:
            produced.update(re.findall(r"attr='(HALT_\w+|REDUCE_\w+|PASS)'", ast.dump(node)))

    return [
        f"src/risk/gates.py GateStatus.{m} can never be emitted by evaluate_all_gates()"
        for m in members
        if m != "PASS" and m not in produced
    ]


CHECKS = (
    ("import cycles", check_import_cycles),
    ("positional column slices", check_positional_column_slices),
    ("non-strict zip", check_zip_is_strict),
    ("orphan asyncio tasks", check_no_orphan_tasks),
    ("unread settings", check_settings_are_read),
    ("unreachable gate statuses", check_every_gate_status_is_reachable),
)


def main() -> int:
    failed = 0
    for label, check in CHECKS:
        problems = check()
        status = "FAIL" if problems else "ok"
        print(f"[{status:4}] {label}")
        for problem in problems:
            print(f"         {problem}")
        failed += len(problems)
    print(f"\n{failed} violation(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
