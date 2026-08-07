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


def _migration_versions(path: Path, list_name: str) -> list[tuple[int, int]]:
    """(version, lineno) for every tuple literal in a migration list."""
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id != list_name or not isinstance(node.value, (ast.List, ast.Tuple)):
            continue
        return [
            (entry.elts[0].value, entry.lineno)
            for entry in node.value.elts
            if isinstance(entry, ast.Tuple)
            and entry.elts
            and isinstance(entry.elts[0], ast.Constant)
            and isinstance(entry.elts[0].value, int)
        ]
    return []


def check_migration_versions() -> list[str]:
    """
    Two migrations sharing a version number is unrecoverable in the field.

    Migrations are applied forward-only and skipped once the recorded schema
    version is at or past them, so a database that applied one of a colliding
    pair will never apply the other — and nothing reports it. Two branches
    open at once both taking the next free number is the normal way this
    happens; it happened here on 2026-08-01 with two v6 migrations.

    The SQLite and TimescaleDB backends must also stay in lockstep: they are
    interchangeable behind AnyStorageBackend, so a version present in one and
    absent from the other means the same code meets two different schemas.
    """
    backends = (
        (SRC / "data" / "storage.py", "_MIGRATIONS"),
        (SRC / "data" / "timescale_storage.py", "_PG_MIGRATIONS"),
    )
    problems: list[str] = []
    seen: dict[str, set[int]] = {}

    for path, list_name in backends:
        versions = _migration_versions(path, list_name)
        if not versions:
            problems.append(f"{_rel(path)}: could not read {list_name} — check moved or renamed")
            continue
        counts: dict[int, list[int]] = defaultdict(list)
        for version, lineno in versions:
            counts[version].append(lineno)
        for version, lines in sorted(counts.items()):
            if len(lines) > 1:
                at = ", ".join(f"line {line}" for line in lines)
                problems.append(f"{_rel(path)}: migration version {version} defined twice ({at})")
        seen[_rel(path)] = {v for v, _ in versions}

    if len(seen) == 2:
        (a_name, a), (b_name, b) = seen.items()
        problems.extend(f"migration {m} is in {a_name} but not {b_name}" for m in sorted(a - b))
        problems.extend(f"migration {m} is in {b_name} but not {a_name}" for m in sorted(b - a))
    return problems


def check_enum_members_exist() -> list[str]:
    """
    ``SomeEnum.RENAMED_AWAY`` raises AttributeError only when that line runs.

    Renaming a member is a one-line change in the enum and an unbounded
    number of call sites elsewhere, and nothing links them. Two branches
    make it worse: one renames, the other adds a reference to the old name,
    and the merge is textually clean. That is exactly how
    ``DiscrepancyType.MISSING_ON_EXCHANGE`` survived into a merged branch.
    """
    members: dict[str, set[str]] = {}
    for path in _py_files(SRC):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
            bases |= {b.attr for b in node.bases if isinstance(b, ast.Attribute)}
            if not bases & {"Enum", "StrEnum", "IntEnum", "Flag", "IntFlag"}:
                continue
            members[node.name] = {
                target.id
                for stmt in node.body
                for target in (
                    stmt.targets
                    if isinstance(stmt, ast.Assign)
                    else [stmt.target]
                    if isinstance(stmt, ast.AnnAssign)
                    else []
                )
                if isinstance(target, ast.Name)
            }

    problems: list[str] = []
    for root in (SRC, REPO / "tests"):
        for path in _py_files(root):
            for node in ast.walk(_parse(path)):
                if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                    continue
                known = members.get(node.value.id)
                # Only flag SHOUTY_CASE, which is unambiguously a member
                # reference; lowercase attributes are methods and properties
                # that Enum subclasses are free to define.
                if known and node.attr.isupper() and node.attr not in known:
                    problems.append(
                        f"{_rel(path)}:{node.lineno}: "
                        f"{node.value.id}.{node.attr} is not a member of {node.value.id}"
                    )
    return problems


def _keyword_only_safe_signatures() -> dict[str, set[str] | None]:
    """
    Accepted keyword names per ``ClassName.__init__`` / top-level function.

    None means "accepts anything" — the target takes ``**kwargs``, so no
    conclusion can be drawn and the call is not checked.
    """
    accepted: dict[str, set[str] | None] = {}
    for path in _py_files(SRC):
        tree = _parse(path)
        targets: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                targets.append((node.name, node))
            elif isinstance(node, ast.ClassDef):
                targets.extend(
                    (node.name, stmt)
                    for stmt in node.body
                    if isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__"
                )
        for name, fn in targets:
            if fn.args.kwarg is not None:
                accepted[name] = None
                continue
            names = {a.arg for a in (*fn.args.args, *fn.args.posonlyargs, *fn.args.kwonlyargs)}
            names.discard("self")
            # A name defined in two modules with different signatures cannot
            # be resolved without real import machinery, so it is dropped
            # rather than guessed at.
            accepted[name] = None if name in accepted and accepted[name] != names else names
    return accepted


def check_keyword_arguments_match_signatures() -> list[str]:
    """
    ``Thing(renamed_kwarg=...)`` is a TypeError only on the line that runs.

    Same shape as the enum check and the same origin: a constructor keyword
    renamed on one branch while another branch adds call sites using the old
    name. ``OptionsCarryStrategy(caps=...)`` against a signature that had
    become ``greeks_caps=`` cost a full CI round on 2026-08-01.

    Resolution is by bare name, not by import, so anything ambiguous across
    modules is skipped rather than guessed.
    """
    accepted = _keyword_only_safe_signatures()
    problems: list[str] = []
    for root in (SRC, REPO / "tests"):
        for path in _py_files(root):
            for node in ast.walk(_parse(path)):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                allowed = accepted.get(node.func.id)
                if not allowed:  # unknown target, or **kwargs, or no named params
                    continue
                problems.extend(
                    f"{_rel(path)}:{node.lineno}: {node.func.id}() has no parameter {keyword.arg!r}"
                    for keyword in node.keywords
                    if keyword.arg is not None and keyword.arg not in allowed
                )
    return problems


def _dataclass_attributes() -> dict[str, set[str] | None]:
    """
    Attribute names reachable on each ``@dataclass`` defined under ``src``.

    Fields plus anything the class defines itself. None means "do not check"
    — the name is defined twice with different shapes, or the class has a
    base other than ``object``, so inherited attributes are unknowable here.
    """
    known: dict[str, set[str] | None] = {}
    for path in _py_files(SRC):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ClassDef):
                continue
            decorators = {
                d.id if isinstance(d, ast.Name) else d.func.id
                for d in node.decorator_list
                if isinstance(d, ast.Name)
                or (isinstance(d, ast.Call) and isinstance(d.func, ast.Name))
            }
            if "dataclass" not in decorators:
                continue
            if node.bases:  # inherited attributes are not visible from here
                known[node.name] = None
                continue
            attrs = {
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            }
            attrs |= {
                stmt.name
                for stmt in node.body
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            known[node.name] = None if node.name in known and known[node.name] != attrs else attrs
    return known


def _locals_typed_as_dataclasses(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, known: dict[str, set[str] | None]
) -> dict[str, str]:
    """
    Map local name → dataclass name, for bindings whose type is stated outright.

    Only three unambiguous forms are read: ``x = Foo(...)``, ``x: Foo``/
    ``x: list[Foo]``, and iterating or ``max``/``min``-ing a name already
    known to hold ``list[Foo]``. Anything reassigned to something else is
    dropped rather than guessed at.
    """
    holds: dict[str, str] = {}  # name → dataclass, for a single instance
    lists: dict[str, str] = {}  # name → dataclass, for list[dataclass]
    dropped: set[str] = set()

    def elt_of(annotation: ast.expr) -> str | None:
        """``list[Foo]`` → "Foo"."""
        if (
            isinstance(annotation, ast.Subscript)
            and isinstance(annotation.value, ast.Name)
            and annotation.value.id in ("list", "List")
            and isinstance(annotation.slice, ast.Name)
        ):
            return annotation.slice.id
        return None

    def bind(target: ast.expr, dataclass_name: str | None) -> None:
        if not isinstance(target, ast.Name):
            return
        if dataclass_name is None or known.get(dataclass_name) is None:
            dropped.add(target.id)
            holds.pop(target.id, None)
            return
        if target.id in holds and holds[target.id] != dataclass_name:
            dropped.add(target.id)
            return
        holds[target.id] = dataclass_name

    for node in ast.walk(fn):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            element = elt_of(node.annotation)
            if element is not None:
                lists[node.target.id] = element
            elif isinstance(node.annotation, ast.Name):
                bind(node.target, node.annotation.id)
            else:
                bind(node.target, None)
        elif isinstance(node, ast.Assign):
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                if value.func.id in ("max", "min") and value.args:
                    first = value.args[0]
                    source = lists.get(first.id) if isinstance(first, ast.Name) else None
                    for target in node.targets:
                        bind(target, source)
                else:
                    for target in node.targets:
                        bind(target, value.func.id if value.func.id in known else None)
            else:
                for target in node.targets:
                    bind(target, None)
        elif (isinstance(node, (ast.For, ast.AsyncFor)) and isinstance(node.iter, ast.Name)) or (
            isinstance(node, ast.comprehension) and isinstance(node.iter, ast.Name)
        ):
            bind(node.target, lists.get(node.iter.id))

    return {name: cls for name, cls in holds.items() if name not in dropped}


def check_dataclass_attributes_exist() -> list[str]:
    """
    ``instance.renamed_field`` raises AttributeError only when that line runs.

    Same failure mode as the enum and keyword checks, one level down: a
    dataclass field renamed in one module against readers in another that
    nothing links. ``src.intel`` read ``best.horizon_idx`` off a
    ``WorkerResult`` whose field is ``horizon_id``, which made every
    ``on_bar()`` call raise after fusion — the live signal path could not
    emit at all, and no test covered it.
    """
    known = _dataclass_attributes()
    problems: list[str] = []
    for root in (SRC, REPO / "tests"):
        for path in _py_files(root):
            for fn in ast.walk(_parse(path)):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                typed = _locals_typed_as_dataclasses(fn, known)
                if not typed:
                    continue
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                        continue
                    cls = typed.get(node.value.id)
                    attrs = known.get(cls) if cls else None
                    if attrs is not None and node.attr not in attrs:
                        problems.append(
                            f"{_rel(path)}:{node.lineno}: {cls} has no attribute {node.attr!r}"
                        )
    return problems


CHECKS = (
    ("import cycles", check_import_cycles),
    ("positional column slices", check_positional_column_slices),
    ("non-strict zip", check_zip_is_strict),
    ("orphan asyncio tasks", check_no_orphan_tasks),
    ("unread settings", check_settings_are_read),
    ("unreachable gate statuses", check_every_gate_status_is_reachable),
    ("migration versions", check_migration_versions),
    ("enum members", check_enum_members_exist),
    ("keyword arguments", check_keyword_arguments_match_signatures),
    ("dataclass attributes", check_dataclass_attributes_exist),
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
