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

Known limits — read these before trusting a clean run:

  * No import-alias tracking. `from datetime import datetime as dt` then
    `dt.now() - x` is not recognised as a wall-clock duration, and neither
    is any other renamed import. Every check matches on the name as written.
  * No cross-procedural analysis. A caller that exists but is itself dead
    code still satisfies check_protocol_methods_are_called; a value that
    reaches a sink through a helper is not traced.
  * Name-based, not type-based. `.columns` on something that is not a
    DataFrame matches; a DataFrame reached under another attribute name
    does not.
  * These are floors, not proofs. Six of the checks here were found, by
    probing them with alternative spellings of their own defect, to miss the
    most obvious variant of what they were written to detect — after having
    reported clean for many commits. A passing run means the specific
    patterns below were not found. It does not mean the defect is absent.

    Probe a new check with the defect written three different ways before
    trusting it.
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


def _slices_axis_labels(value: ast.AST) -> bool:
    """
    Whether `value` is a DataFrame/Series axis-label sequence.

    Matches `.columns` / `.index` directly, and through the two wrappers that
    produce the same positional list: `list(df.columns)` and
    `df.columns.tolist()`. Both were invisible to this check until it was
    probed with them, and both are the more natural spelling once a caller
    wants a real list rather than an Index.
    """
    if isinstance(value, ast.Call):
        func = value.func
        # df.columns.tolist()
        if isinstance(func, ast.Attribute) and func.attr in ("tolist", "to_list"):
            return _slices_axis_labels(func.value)
        # list(df.columns)
        if isinstance(func, ast.Name) and func.id == "list" and value.args:
            return _slices_axis_labels(value.args[0])
        return False
    return isinstance(value, ast.Attribute) and value.attr in ("index", "columns")


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
            if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)):
                continue
            if not _slices_axis_labels(node.value):
                continue
            key = (_rel(path), owner.get(node, "<module>"))
            if key not in _POSITIONAL_SLICE_ALLOWED:
                problems.append(
                    f"{_rel(path)}:{node.lineno} positional slice of axis labels "
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
    """
    A discarded create_task swallows its exception until garbage collection.

    asyncio.ensure_future() has the identical failure mode and was invisible
    to this check until probed for.
    """
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
            and node.func.attr in ("create_task", "ensure_future")
            and isinstance(parents.get(node), ast.Expr)
        )
    return problems


def check_settings_are_read() -> list[str]:
    """
    A setting nothing reads is a promise the software does not keep.

    Two of these were load-bearing when first scanned: half of a self-tuning
    cadence guard, and a bar-retention window whose pruner had no caller.

    Readers are src/ and scripts/ — deliberately NOT tests/. Counting a test
    read let a setting satisfy this check while the software still ignored
    it, which is precisely the promise the check exists to enforce: a test
    asserting a default exists proves nothing about anything consuming it.
    Verified before narrowing that every setting still resolves, so this
    closes the loophole without a single false positive.

    src/config.py counts as a reader. RuntimeConfig.__init__ seeds the
    exit-control overlay (stop_loss_pct_default and its five siblings) from
    the settings there, and the URL validators consume base_url/ws_url — all
    genuine consumption that happens to live in the declaring module.
    """
    known_decorative = {
        "log_as_json",
        # ccxt's own limiter governs these. Verified: enableRateLimit=True is
        # set at every exchange construction site — _build_binance and
        # _build_okx in src/data/fetcher.py, and the router's instance in
        # src/execution/router.py — so these settings genuinely have no
        # consumer rather than having lost one.
        "rate_limit_per_minute",
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
    for root in ("src", "scripts"):
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


def check_protocol_methods_are_called() -> list[str]:
    """
    Every method a Protocol declares must be called somewhere in src/.

    This is the check that would have caught the largest defect this script's
    own docstring describes. `StrategyProtocol` declared `generate_signal`,
    seven families implemented it, a registry collected them, a capital
    allocator weighted them and a kill switch could disable them — and
    nothing in the process ever called it. The portfolio was inert on the one
    axis that mattered while looking fully wired on every other.

    A Protocol is a contract with two sides. Implementations are easy to spot
    and easy to test in isolation, which is exactly why the missing side goes
    unnoticed: every unit test passes, coverage is high, and the method is
    simply never reached at runtime. Declaring a method nobody calls means
    either the caller was never written or the Protocol outlived its purpose;
    both are worth a line of output.

    Deliberately permissive about *how* the call happens — any `.name(` on any
    object counts, since a Protocol exists precisely so callers need not know
    the concrete type. That means this cannot detect a caller that is itself
    dead code, only one that does not exist at all. It is a floor, not a
    guarantee.

    Dunder methods are exempt: they are invoked by syntax (`len(x)`, `x in y`)
    rather than by name, so an attribute-call scan cannot see them.

    Properties are checked as attribute *accesses* rather than calls — a
    Protocol property is consumed as `obj.name`, and demanding `obj.name()`
    would report every one of them as dead.
    """
    # name -> (location, is_property)
    declared: dict[str, tuple[str, bool]] = {}
    for path in _py_files(SRC):
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            def _names_protocol(base: ast.AST) -> bool:
                # Protocol, typing.Protocol, Protocol[T] and typing.Protocol[T].
                # The subscripted generic form was invisible until probed for.
                if isinstance(base, ast.Subscript):
                    base = base.value
                if isinstance(base, ast.Name):
                    return base.id == "Protocol"
                return isinstance(base, ast.Attribute) and base.attr == "Protocol"

            is_protocol = any(_names_protocol(base) for base in node.bases)
            if not is_protocol:
                continue
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name.startswith("__"):
                    continue
                is_property = any(
                    (isinstance(d, ast.Name) and d.id == "property")
                    or (isinstance(d, ast.Attribute) and d.attr == "property")
                    for d in item.decorator_list
                )
                declared.setdefault(
                    item.name,
                    (f"{_rel(path)}:{item.lineno} {node.name}.{item.name}", is_property),
                )

    called: set[str] = set()
    accessed: set[str] = set()
    for path in _py_files(SRC):
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Attribute):
                accessed.add(node.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)

    problems: list[str] = []
    for name, (location, is_property) in sorted(declared.items()):
        used = accessed if is_property else called
        if name in used:
            continue
        verb = "read" if is_property else "called"
        problems.append(f"{location} — declared on a Protocol but never {verb} anywhere in src/")
    return problems


# Dataclass fields that nothing reads, each with the reason it is tolerated.
# These are outputs of subsystems that are themselves not yet wired — the
# field is dead because its consumer does not exist, not because the field is
# wrong. They are listed rather than deleted so the count cannot grow
# silently: a NEW unread field is a wiring mistake and should fail here.
_UNREAD_FIELD_ALLOWED = {
    # KyberKeyPair / KyberCiphertext belong to pq_transport, a Kyber-768
    # transport stub with no production consumer: PQTransportStub reports
    # itself unavailable and raises on keygen. These are the shapes the real
    # implementation will fill in, so nothing reads them back yet.
    "encapsulation_key",
    "decapsulation_key",
    "ciphertext",
    "shared_secret",
    # TripleBarrierResult remains unconstructed — the per-observation record
    # is superseded by TripleBarrierComposition, which aggregates the same
    # exit information. Kept because AFML Ch.4 sample-uniqueness weighting
    # needs the per-observation form, which is a modelling change requiring
    # out-of-sample validation rather than a wiring fix.
    "exit_index",
    # ProbabilisticPrediction has no consumer anywhere in src/. The whole
    # uncertainty layer is inert; these are its unconsumed outputs.
    "posterior_samples",
    "model_uncertainty",
    # shadow_deploy records these but nothing reads them back. `predictions`
    # joined the list once bare string literals stopped counting as reads —
    # it was never accessed as an attribute, only matched by a coincidental
    # "predictions" elsewhere, which is precisely the masking that allowance
    # permitted. All three belong to ModelRecord: the A/B harness fills them
    # and no consumer exists.
    "actuals",
    "evaluated_at",
    "predictions",
}


def check_dataclass_fields_are_read() -> list[str]:
    """
    A dataclass field nothing reads is data the software collects and drops.

    Same connectivity shape as the Protocol check, one level down. It found
    RiskGateContext.whale_scalar, whose own comment claimed it was set by
    evaluate_all_gates() and returned in details — it was neither, and the
    50% size reduction it promised had no implementation at all while the
    gate hard-vetoed instead.

    Reads are counted across src/ AND tests/, and keyword arguments and
    string literals count too: a field consumed only as a constructor kwarg,
    a serialisation key, or a dict lookup is genuinely used. That makes this
    deliberately permissive — it catches fields with no consumer at all, not
    fields with a weak one.

    Private fields (leading underscore) are exempt as implementation detail.
    """
    declared: dict[str, str] = {}
    for path in _py_files(SRC):
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            is_dataclass = any(
                (isinstance(d, ast.Name) and d.id == "dataclass")
                or (isinstance(d, ast.Attribute) and d.attr == "dataclass")
                or (
                    isinstance(d, ast.Call)
                    and (
                        (isinstance(d.func, ast.Name) and d.func.id == "dataclass")
                        or (isinstance(d.func, ast.Attribute) and d.func.attr == "dataclass")
                    )
                )
                for d in node.decorator_list
            )
            if not is_dataclass:
                continue
            for stmt in node.body:
                if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                    continue
                name = stmt.target.id
                if name.startswith("_"):
                    continue
                declared.setdefault(name, f"{_rel(path)}:{stmt.lineno} {node.name}.{name}")

    read: set[str] = set()
    for root in ("src", "tests"):
        for path in _py_files(REPO / root):
            for node in ast.walk(_parse(path)):
                if isinstance(node, ast.Attribute):
                    read.add(node.attr)
                elif isinstance(node, ast.keyword) and node.arg:
                    read.add(node.arg)
                # A bare string equal to the field name used to count as a
                # read. It is too weak a proxy: any log key, dict key or
                # message fragment that happens to match rescues a field
                # nothing touches. Measured across all 530 fields, exactly
                # one was surviving on that alone — ModelRecord.predictions,
                # in a class whose two sibling fields were already known dead
                # — so dropping it costs no real coverage and removes a way
                # for a genuinely unread field to look read.

    return [
        f"{location} — dataclass field never read anywhere in src/ or tests/"
        for name, location in sorted(declared.items())
        if name not in read and name not in _UNREAD_FIELD_ALLOWED
    ]


def check_no_silent_broad_except() -> list[str]:
    """
    A broad `except` whose whole body is `pass` erases the failure forever.

    Narrow handlers are exempt and deliberately so — `except ImportError:
    pass` around an optional dependency, or `except OSError: pass` around a
    syscall that does not exist on every platform, is control flow, and the
    exception type documents the intent. What this catches is the broad
    kind: `except Exception: pass` names no expectation, so it swallows the
    failure it was written for and every unrelated bug that ever lands in
    the same block, with nothing to distinguish them.

    The distinction matters most where this codebase already gets it right.
    The orchestrator's metrics handler catches broadly and logs at warning,
    with a comment saying a silent pass would hide a real bug indefinitely
    because Prometheus gives no feedback loop back into the process. That is
    the standard; this check is what keeps it from eroding.

    `continue`, a bare `return`, and `...` all count as a pass — each
    discards the exception just as completely. They were added after probing
    this check with the alternative spellings of its own defect: it knew only
    `pass`, so three ways of writing the same thing walked straight past it.
    """
    problems: list[str] = []
    for path in _py_files(SRC):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ExceptHandler):
                continue
            caught = node.type
            is_broad = caught is None or (
                isinstance(caught, ast.Name) and caught.id in ("Exception", "BaseException")
            )
            if not is_broad:
                continue
            body = [
                stmt
                for stmt in node.body
                if not (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                )
            ]
            swallowed = False
            if len(body) == 1:
                only = body[0]
                swallowed = (
                    isinstance(only, (ast.Pass, ast.Continue))
                    or (isinstance(only, ast.Return) and only.value is None)
                    or (
                        isinstance(only, ast.Expr)
                        and isinstance(only.value, ast.Constant)
                        and only.value.value is Ellipsis
                    )
                )
            if swallowed:
                caught_name = getattr(caught, "id", None)
                name = "bare except" if caught is None else f"except {caught_name}"
                problems.append(
                    f"{_rel(path)}:{node.lineno} {name} with no handling — "
                    "log it, or narrow the exception type to say what was expected"
                )
    return problems


def check_datetimes_are_timezone_aware() -> list[str]:
    """
    Naive datetimes, on a codebase whose domain prior is "UTC timestamps".

    A naive datetime is not wrong where it is created — it is wrong wherever
    it is later compared, subtracted, or serialised next to an aware one. On
    a machine running in UTC every test passes and the bug is latent until
    the process runs somewhere else; comparing the two raises TypeError, and
    subtracting them silently yields an offset-sized error in a duration.

    This currently finds nothing: every site in src/ already passes a
    timezone, several of them positionally as fromtimestamp(0, UTC). The
    check exists to keep it that way, because the failure surfaces far from
    its cause and nothing in ruff or mypy looks for it.
    """
    problems: list[str] = []
    for path in _py_files(SRC):
        for node in ast.walk(_parse(path)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            attr = node.func.attr
            has_tz_kwarg = any(k.arg == "tz" for k in node.keywords)
            if attr == "utcnow":
                problems.append(
                    f"{_rel(path)}:{node.lineno} datetime.utcnow() is naive and deprecated "
                    "— use datetime.now(tz=UTC)"
                )
            elif attr == "now" and not has_tz_kwarg and not node.args:
                problems.append(f"{_rel(path)}:{node.lineno} datetime.now() without a timezone")
            elif attr == "fromtimestamp" and not has_tz_kwarg and len(node.args) < 2:
                # tz is the second POSITIONAL parameter, so two args is aware.
                problems.append(
                    f"{_rel(path)}:{node.lineno} datetime.fromtimestamp() without a timezone"
                )
    return problems


_MUTABLE_FACTORIES = frozenset(
    {"list", "dict", "set", "defaultdict", "OrderedDict", "Counter", "deque"}
)


_AUTH_CALL_NAMES = frozenset({"verify_ws_key", "api_key_header", "require_permission"})


def check_every_route_is_authenticated() -> list[str]:
    """
    Every HTTP route and WebSocket must authenticate.

    This one currently passes, and is here because the failure is silent in
    the worst direction: a new endpoint added without `dependencies=` serves
    to anyone who can reach the port, and nothing in the response, the logs
    or the type checker distinguishes it from a guarded one. With ~30 routes
    on this app, the omission is a plausible slip rather than a hypothetical.

    Two spellings count as authenticated. Most routes declare
    `dependencies=[Depends(api_key_header), ...]`. The WebSocket instead
    awaits verify_ws_key() as its first statement, deliberately: auth has to
    happen before add_ws_client(), or a raising auth leaks a client slot.
    Demanding the decorator form would report that as unguarded and push
    someone to "fix" a correct ordering.
    """
    main = SRC / "api" / "main.py"
    if not main.exists():
        return []

    problems: list[str] = []
    for node in ast.walk(_parse(main)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr not in (
                "get",
                "post",
                "put",
                "delete",
                "patch",
                "websocket",
            ):
                continue
            if any(kw.arg == "dependencies" for kw in dec.keywords):
                continue
            body_authenticates = any(
                isinstance(call, ast.Call)
                and (
                    (isinstance(call.func, ast.Name) and call.func.id in _AUTH_CALL_NAMES)
                    or (isinstance(call.func, ast.Attribute) and call.func.attr in _AUTH_CALL_NAMES)
                )
                for call in ast.walk(node)
            )
            if body_authenticates:
                continue
            route = (
                dec.args[0].value
                if dec.args and isinstance(dec.args[0], ast.Constant)
                else "<unknown>"
            )
            problems.append(
                f"{_rel(main)}:{node.lineno} {dec.func.attr.upper()} {route} has no "
                "dependencies= guard and no in-body auth call — it serves unauthenticated"
            )
    return problems


def check_docstrings_do_not_cite_missing_modules() -> list[str]:
    """
    A docstring naming a `*.py` that does not exist is a false map.

    This found src/features/derivatives.py claiming to consolidate data from
    deribit_provider.py, which has never existed here — and that absence is
    exactly why options_carry_v1 is inert, since nothing in this process
    fetches an implied-vol surface. A reader chasing that filename would
    conclude the feed existed and the wiring was the problem.

    Resolution spans src/ and scripts/ because the two reference each other
    (scheduler cites scripts/run_tuning_attempt.py, which is real). Scoping
    this to src/ alone produced six false positives on the first pass, and a
    check that cries wolf on real files gets ignored rather than fixed.

    Glob spellings like `*_provider.py` are skipped: they name a family, not
    a file, and expanding them would re-introduce the same false positives.
    """
    known: set[str] = set()
    for root in ("src", "scripts"):
        directory = REPO / root
        if directory.exists():
            known |= {p.name for p in _py_files(directory)}

    # (?<![*\w]) rejects the tail of a glob: `*_provider.py` names a family,
    # and capturing `_provider.py` out of it reports a file nobody claimed
    # exists. That was three of the first four hits.
    cited = re.compile(r"(?<![*\w])([A-Za-z][A-Za-z0-9_]{2,})\.py\b")
    problems: list[str] = []
    for path in _py_files(SRC):
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            doc = ast.get_docstring(node)
            if not doc:
                continue
            for name in dict.fromkeys(cited.findall(doc)):
                if name.startswith("*") or f"{name}.py" in known:
                    continue
                # ast.Module carries no lineno; its docstring is at the top.
                line = getattr(node, "lineno", 1)
                problems.append(
                    f"{_rel(path)}:{line} docstring cites {name}.py, "
                    "which does not exist in src/ or scripts/"
                )
    return problems


def check_no_mutable_class_attributes() -> list[str]:
    """
    A mutable class attribute is one object shared by every instance.

    ruff's RUF012 covers this and is in the project ignore list — the same
    gap that left B006 unguarded — so nothing else looks for it.

    It found OrderFSM._VALID_TRANSITIONS, the table deciding whether an
    order may legally change state. Its own comment said Final prevents
    rebinding the dict but not editing what is inside it, and left it
    writable regardless: one write anywhere in the process could have given
    a terminal order state an exit, for every OrderFSM at once.

    Empty literals are exempt — they are the conventional spelling of "this
    class has no entries by default", and dataclasses are skipped because
    field(default_factory=...) is their answer and is checked separately by
    the mutable-defaults rule.
    """
    problems: list[str] = []
    for path in _py_files(SRC):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ClassDef):
                continue
            is_dataclass = any("dataclass" in ast.dump(dec) for dec in node.decorator_list)
            if is_dataclass:
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign):
                    target, value = stmt.target, stmt.value
                elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target, value = stmt.targets[0], stmt.value
                else:
                    continue
                if not isinstance(value, (ast.List, ast.Dict, ast.Set)):
                    continue
                populated = value.keys if isinstance(value, ast.Dict) else value.elts
                if not populated:
                    continue
                name = getattr(target, "id", "<attr>")
                problems.append(
                    f"{_rel(path)}:{stmt.lineno} {node.name}.{name} is a mutable class "
                    "attribute shared by every instance — wrap it in MappingProxyType, "
                    "use a tuple/frozenset, or move it into __init__"
                )
    return problems


def check_no_mutable_default_arguments() -> list[str]:
    """
    A mutable default is evaluated once, at definition, and shared forever.

    ruff's B006 covers this and is in this project's ignore list, so nothing
    else checks it. That was a reasonable call for the false-positive rate on
    B008-style patterns, but it leaves the genuine version unguarded: a
    default list or dict that one caller mutates is visible to every
    subsequent caller, and in a long-running trading process that means
    state leaking across ticks with no obvious source.

    Currently finds nothing — every default in src/ is immutable or built
    with field(default_factory=...). The check is here so the ignored rule
    still has a floor.

    The factory list covers defaultdict/OrderedDict/Counter/deque as well as
    the builtins: each is just as shared across calls, and each was invisible
    to this check until it was probed with them.
    """
    problems: list[str] = []
    for path in _py_files(SRC):
        for node in ast.walk(_parse(path)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            defaults = [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]
            for default in defaults:
                literal = isinstance(default, (ast.List, ast.Dict, ast.Set))
                built = (
                    isinstance(default, ast.Call)
                    and isinstance(default.func, ast.Name)
                    and default.func.id in _MUTABLE_FACTORIES
                )
                if literal or built:
                    problems.append(
                        f"{_rel(path)}:{node.lineno} {node.name}() has a mutable default "
                        "— use None and build inside the body"
                    )
    return problems


# Sites where wall-clock arithmetic is correct because the result is a point
# in history rather than an elapsed time. Keyed (file, enclosing function).
_WALL_CLOCK_ARITHMETIC_ALLOWED = {
    # since_ms is an exchange API parameter and must be a real epoch time.
    ("src/engine/universe_returns.py", "_trailing_return"),
    # _locked_until holds an operator-facing "locked until <calendar time>"
    # deadline, which is a point in history by design.
    ("src/tuning/watchdog.py", "_is_locked_unlocked"),
    # Compared against a PERSISTED ISO timestamp read back from disk.
    # time.monotonic() has no meaning across process restarts.
    ("src/tuning/runner.py", "_cooldown_active"),
    # Compared against an ISO timestamp returned by the Dune API. That is an
    # absolute external instant; a monotonic clock has no relationship to it.
    ("src/intelligence/onchain/dune_provider.py", "_results_fresh"),
    # Staleness of an OHLCV bar is measured against the exchange's own
    # timestamp_utc, an absolute external instant. A monotonic clock has no
    # relationship to it.
    ("src/data/quality_gate.py", "check_ohlcv"),
    # Same: compared against the macro release date parsed from the provider.
    ("src/data/quality_gate.py", "check_macro"),
    # since_ms is an exchange API parameter and must be a real epoch time.
    ("src/engine/orchestrator.py", "_tick"),
}


def check_durations_use_monotonic() -> list[str]:
    """
    time.time() subtracted or compared is a duration on the wrong clock.

    Wall clock is correct for recording WHEN something happened and wrong for
    measuring HOW LONG it took, because it is not monotonic: NTP steps it in
    both directions. A backward correction holds a deadline open past its
    timeout and keeps a stale cache alive; a forward one expires every TTL at
    once and can satisfy an elapsed-time gate before the time has elapsed.
    None of it raises, and none of it reproduces on a developer machine.

    Four real instances existed when this was written: a 5s collection
    deadline on the signal path, a provider TTL cache, the shadow-deployment
    age gate that decides when a challenger model may be promoted, and the
    universe cache's own TTL and backoff.

    Recording a timestamp is untouched — `ts=int(time.time() * 1000)` is
    right. Only arithmetic and comparison are flagged, which is the syntactic
    shape of "using this as a duration". The one legitimate exception in this
    repository is allowlisted rather than special-cased, because computing a
    point in history by subtraction is a real and correct thing to do.
    """

    def _is_wall_clock(node: ast.AST) -> bool:
        """time.time() or datetime.now(...) — both are wall clocks."""
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            return False
        if node.func.attr == "time":
            return isinstance(node.func.value, ast.Name) and node.func.value.id == "time"
        # datetime.now(UTC) - other is just as wall-clock as time.time() - other,
        # and reads more innocently. The intelligence client's Glassnode
        # throttle measured its interval that way and this check missed it
        # until the datetime form was added.
        if node.func.attr in ("now", "utcnow"):
            owner = node.func.value
            # `from datetime import datetime` -> Name('datetime')
            if isinstance(owner, ast.Name):
                return owner.id == "datetime"
            # `import datetime`              -> Attribute(Name('datetime'), 'datetime')
            return isinstance(owner, ast.Attribute) and owner.attr == "datetime"
        return False

    problems: list[str] = []
    for path in _py_files(SRC):
        tree = _parse(path)
        owner = _enclosing_function(tree)
        seen: set[int] = set()
        for node in ast.walk(tree):
            used_as_duration = False
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
                used_as_duration = _is_wall_clock(node.left) or _is_wall_clock(node.right)
            elif isinstance(node, ast.Compare):
                used_as_duration = _is_wall_clock(node.left) or any(
                    _is_wall_clock(c) for c in node.comparators
                )
            if not used_as_duration or node.lineno in seen:
                continue
            seen.add(node.lineno)
            if (_rel(path), owner.get(node, "<module>")) in _WALL_CLOCK_ARITHMETIC_ALLOWED:
                continue
            problems.append(
                f"{_rel(path)}:{node.lineno} wall clock used as a duration — "
                "use time.monotonic(), or allowlist it if the result is a point in history"
            )
    return problems


# Functions this repository has itself documented as CPU-bound. Calling one
# directly inside an async def puts it on the event loop.
_CPU_BOUND_FUNCTIONS = frozenset({"build_feature_matrix"})


def check_cpu_bound_work_is_offloaded() -> list[str]:
    """
    CPU-bound work called inline inside `async def` blocks the whole loop.

    One event loop serves all three timeframe tasks, the FastAPI server, the
    position monitor and the order path, so a slow synchronous call in the 1m
    tick does not merely delay that tick — it delays the 15m tick, the API
    and any order in flight. Execution latency is a domain prior here.

    build_feature_matrix earns its place on this list from the repository's
    own comments: the orchestrator hands it to a dedicated executor for
    training, saying "Feature matrix — CPU bound" (NEW-002), and the
    self-tuning scheduler's docstring explains it offloads so "a
    multi-second-to-minutes retrain does not" block. Both were right about
    the cost and both still called it inline on their hot paths — the signal
    engine on every tick, the scheduler immediately before the executor call
    it was already making.

    Calls lexically inside a run_in_executor(...) or to_thread(...) argument
    list are exempt, which is exactly what offloading looks like.
    """
    problems: list[str] = []
    for path in _py_files(SRC):
        for fn in ast.walk(_parse(path)):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            offloaded: set[int] = set()
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("run_in_executor", "to_thread")
                ):
                    for sub in ast.walk(node):
                        offloaded.add(id(sub))
            for node in ast.walk(fn):
                if id(node) in offloaded or not isinstance(node, ast.Call):
                    continue
                # Both `build_feature_matrix(...)` and
                # `pipeline.build_feature_matrix(...)`. Only the bare-name form
                # was recognised until this check was probed with the other.
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                else:
                    name = None
                if name in _CPU_BOUND_FUNCTIONS:
                    problems.append(
                        f"{_rel(path)}:{node.lineno} {name}() called inline in "
                        f"async {fn.name}() — offload with to_thread/run_in_executor"
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
    ("cpu-bound work on the loop", check_cpu_bound_work_is_offloaded),
    ("wall-clock durations", check_durations_use_monotonic),
    ("naive datetimes", check_datetimes_are_timezone_aware),
    ("mutable default arguments", check_no_mutable_default_arguments),
    ("mutable class attributes", check_no_mutable_class_attributes),
    ("docstrings citing missing modules", check_docstrings_do_not_cite_missing_modules),
    ("unauthenticated routes", check_every_route_is_authenticated),
    ("silent broad except", check_no_silent_broad_except),
    ("unread dataclass fields", check_dataclass_fields_are_read),
    ("uncalled protocol methods", check_protocol_methods_are_called),
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
