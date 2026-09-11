"""
Loader and query surface for ``config/quality_registry.json``.

The registry exists so that "we test that" is a checkable claim rather than a
recollection. Every requirement, trading invariant, regression and security
regression gets an id, a testable statement, and a named test file that must
exist on disk. ``docs/quality/REQUIREMENTS_TRACEABILITY.md`` is generated from
it, so the traceability matrix cannot drift from what the tree actually
contains.

Validation is strict and happens at load time, for the same reason it does in
``src.mathcore.registry``: a governance file that has quietly rotted is
indistinguishable from a valid one at the call site, and the cheap moment to
find out is at import, in CI.

The two failure modes this is built to prevent:

  1. **Claiming a test that does not exist.** ``status: verified`` obliges the
     entry to name at least one test file, and the loader stats every one of
     them. A renamed or deleted test turns the claim red instead of silently
     leaving a requirement unguarded.
  2. **Claiming nothing and calling it done.** ``status: planned`` obliges the
     entry to name the PR that will verify it, and that PR must be one of the
     declared phases. An entry cannot sit in limbo without an owner.

The file is read-only at runtime. Nothing here writes to it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = PROJECT_ROOT / "config" / "quality_registry.json"
SCHEMA_PATH = PROJECT_ROOT / "config" / "quality_registry.schema.json"

#: id prefix -> the kind the entry must declare. Binding the two means a
#: mistyped kind cannot hide an invariant among the requirements.
PREFIX_KINDS: dict[str, str] = {
    "INV": "invariant",
    "REG": "regression",
    "SEC": "security_regression",
}

#: Any other prefix (RISK, EXEC, DATA, ...) is a requirement.
DEFAULT_KIND = "requirement"

#: Statuses that oblige the entry to name at least one test.
STATUSES_WITH_TESTS = frozenset({"verified", "partial"})

#: Statuses that oblige the entry to name the PR that finishes the work.
STATUSES_WITH_PHASE = frozenset({"partial", "planned"})

#: Statuses that forbid naming a test, because the entry asserts none exists.
STATUSES_WITHOUT_TESTS = frozenset({"planned", "accepted_gap"})

_ID_PREFIX = re.compile(r"^([A-Z]+)-")


class RegistryError(RuntimeError):
    """Raised when the registry is malformed, inconsistent or self-contradictory."""


@dataclass(frozen=True)
class Verification:
    """One test that stands behind one entry."""

    test: str
    test_type: str
    note: str = ""

    @property
    def path(self) -> str:
        """The file part, with any ``::node`` selector stripped."""
        return self.test.split("::", 1)[0]

    @property
    def node(self) -> str:
        """The ``::``-qualified selector, or "" when the whole file is meant."""
        _, _, node = self.test.partition("::")
        return node


@dataclass(frozen=True)
class Waiver:
    """A deliberate, dated, attributed decision not to verify something."""

    reason: str
    accepted_by: str
    review_by: str


@dataclass(frozen=True)
class RegistryEntry:
    """
    One requirement, invariant, regression or security regression.

    Frozen: a requirement whose status can be mutated at runtime is not a
    control. To change a status, edit the JSON and review the diff.
    """

    id: str
    kind: str
    title: str
    statement: str
    criticality: str
    subsystem: str
    status: str
    source: str
    owning_modules: tuple[str, ...] = ()
    verification: tuple[Verification, ...] = ()
    planned_in: str = ""
    waiver: Waiver | None = None
    depends_on: tuple[str, ...] = ()
    failure_mode: str = ""
    notes: str = ""

    @property
    def is_verified(self) -> bool:
        return self.status == "verified"

    @property
    def is_critical(self) -> bool:
        return self.criticality == "critical"

    @property
    def test_paths(self) -> tuple[str, ...]:
        """Distinct test files behind this entry, in declaration order."""
        seen: list[str] = []
        for ver in self.verification:
            if ver.path not in seen:
                seen.append(ver.path)
        return tuple(seen)

    @property
    def expected_kind(self) -> str:
        match = _ID_PREFIX.match(self.id)
        prefix = match.group(1) if match else ""
        return PREFIX_KINDS.get(prefix, DEFAULT_KIND)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RegistryEntry:
        waiver_raw = raw.get("waiver")
        return cls(
            id=raw["id"],
            kind=raw["kind"],
            title=raw["title"],
            statement=raw["statement"],
            criticality=raw["criticality"],
            subsystem=raw["subsystem"],
            status=raw["status"],
            source=raw["source"],
            owning_modules=tuple(raw.get("owning_modules", [])),
            verification=tuple(
                Verification(test=v["test"], test_type=v["test_type"], note=v.get("note", ""))
                for v in raw.get("verification", [])
            ),
            planned_in=raw.get("planned_in", ""),
            waiver=Waiver(**waiver_raw) if waiver_raw else None,
            depends_on=tuple(raw.get("depends_on", [])),
            failure_mode=raw.get("failure_mode", ""),
            notes=raw.get("notes", ""),
        )


@dataclass(frozen=True)
class QualityRegistry:
    """The whole registry, indexed and validated."""

    registry_version: str
    kind_definitions: dict[str, str]
    status_definitions: dict[str, str]
    criticality_definitions: dict[str, str]
    test_types: dict[str, str]
    subsystems: tuple[str, ...]
    phases: dict[str, str]
    entries: tuple[RegistryEntry, ...]
    _by_id: dict[str, RegistryEntry] = field(repr=False, default_factory=dict)

    # -- lookup ----------------------------------------------------------

    def __iter__(self) -> Iterator[RegistryEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, entry_id: object) -> bool:
        return entry_id in self._by_id

    def get(self, entry_id: str) -> RegistryEntry:
        """Return one entry, or raise with the closest matches to hand."""
        try:
            return self._by_id[entry_id]
        except KeyError:
            near = [k for k in self._by_id if entry_id in k or k in entry_id]
            hint = f" Did you mean: {', '.join(sorted(near)[:3])}?" if near else ""
            raise RegistryError(f"no registry entry {entry_id!r}.{hint}") from None

    def by_kind(self, kind: str) -> tuple[RegistryEntry, ...]:
        return tuple(e for e in self.entries if e.kind == kind)

    def by_status(self, status: str) -> tuple[RegistryEntry, ...]:
        return tuple(e for e in self.entries if e.status == status)

    def by_subsystem(self, subsystem: str) -> tuple[RegistryEntry, ...]:
        return tuple(e for e in self.entries if e.subsystem == subsystem)

    def by_criticality(self, criticality: str) -> tuple[RegistryEntry, ...]:
        return tuple(e for e in self.entries if e.criticality == criticality)

    def by_phase(self, phase: str) -> tuple[RegistryEntry, ...]:
        """Entries whose remaining work is scheduled into ``phase``."""
        if phase not in self.phases:
            raise RegistryError(
                f"unknown phase {phase!r}; declared: {', '.join(sorted(self.phases))}"
            )
        return tuple(e for e in self.entries if e.planned_in == phase)

    def outstanding(self) -> tuple[RegistryEntry, ...]:
        """Everything not yet fully verified, in id order."""
        return tuple(sorted((e for e in self.entries if not e.is_verified), key=lambda e: e.id))

    def tests_for(self, entry_id: str) -> tuple[str, ...]:
        return self.get(entry_id).test_paths

    def entries_for_test(self, test_path: str) -> tuple[RegistryEntry, ...]:
        """Reverse index: which requirements does deleting this file unguard?"""
        return tuple(e for e in self.entries if test_path in e.test_paths)

    # -- graph -----------------------------------------------------------

    def dependencies_of(self, entry_id: str) -> tuple[RegistryEntry, ...]:
        """Transitive dependencies, with cycles already ruled out at load."""
        seen: list[str] = []
        stack = list(self.get(entry_id).depends_on)
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.append(current)
            stack.extend(self.get(current).depends_on)
        return tuple(self.get(i) for i in seen)

    def dependents_of(self, entry_id: str) -> tuple[RegistryEntry, ...]:
        """Entries that name ``entry_id`` directly. Used for impact analysis."""
        self.get(entry_id)  # existence check
        return tuple(e for e in self.entries if entry_id in e.depends_on)

    # -- reporting -------------------------------------------------------

    def expired_waivers(self, today: date) -> tuple[RegistryEntry, ...]:
        """
        Waived entries whose review date has passed.

        A waiver is a decision to accept a risk *for a while*. Past its review
        date it is no longer a decision, it is a habit -- so the expiry is
        queryable rather than buried in the JSON, and CI can fail on it.
        """
        return tuple(
            e
            for e in self.entries
            if e.waiver is not None and date.fromisoformat(e.waiver.review_by) < today
        )

    def coverage_by_status(self) -> dict[str, int]:
        """How many entries sit at each declared status, zeroes included."""
        counts = dict.fromkeys(self.status_definitions, 0)
        for entry in self.entries:
            counts[entry.status] += 1
        return counts


# --------------------------------------------------------------------------
# Loading and validation
# --------------------------------------------------------------------------


def _validate_against_schema(raw: dict[str, Any]) -> None:
    """
    Structural validation. Skipped only if jsonschema is genuinely absent.

    Skipping silently would be worse than failing, so the skip is confined to
    the ImportError: any actual validation failure still raises.
    """
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema is a declared dependency
        return

    with SCHEMA_PATH.open(encoding="utf-8") as fh:
        schema = json.load(fh)

    validator = jsonschema.Draft7Validator(schema)
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.path) or "<root>"
        raise RegistryError(
            f"{REGISTRY_PATH.name} fails its schema at {location}: {first.message} "
            f"({len(errors)} error(s) total)"
        )


def _check_vocabularies(raw: dict[str, Any], entries: list[RegistryEntry]) -> None:
    """Every free-form string an entry uses must come from a declared vocabulary."""
    vocab: list[tuple[str, set[str], str]] = [
        ("kind", set(raw["kind_definitions"]), "kind_definitions"),
        ("status", set(raw["status_definitions"]), "status_definitions"),
        ("criticality", set(raw["criticality_definitions"]), "criticality_definitions"),
        ("subsystem", set(raw["subsystems"]), "subsystems"),
    ]
    for entry in entries:
        for attribute, allowed, source in vocab:
            value = getattr(entry, attribute)
            if value not in allowed:
                raise RegistryError(
                    f"entry {entry.id!r} uses {attribute}={value!r}, which is not declared "
                    f"in {source}"
                )

    known_types = set(raw["test_types"])
    for entry in entries:
        for ver in entry.verification:
            if ver.test_type not in known_types:
                raise RegistryError(
                    f"entry {entry.id!r} verifies with test_type={ver.test_type!r}, which is "
                    f"not in the declared test taxonomy"
                )


def _check_identity(entries: list[RegistryEntry]) -> None:
    """Ids are unique, and the id prefix agrees with the declared kind."""
    ids = [e.id for e in entries]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise RegistryError(f"duplicate entry ids: {', '.join(duplicates)}")

    for entry in entries:
        if entry.kind != entry.expected_kind:
            raise RegistryError(
                f"entry {entry.id!r} declares kind={entry.kind!r} but its id prefix means "
                f"{entry.expected_kind!r}. The prefix and the kind are one fact, not two."
            )


def _check_status_contract(raw: dict[str, Any], entries: list[RegistryEntry]) -> None:
    """
    The heart of the registry: what each status obliges the entry to carry.

    Without this, ``verified`` is a word someone typed. With it, ``verified``
    means a named test file is on disk, and ``planned`` means a named PR owns
    the gap.
    """
    phases = set(raw["phases"])
    for entry in entries:
        has_tests = bool(entry.verification)

        if entry.status in STATUSES_WITH_TESTS and not has_tests:
            raise RegistryError(
                f"entry {entry.id!r} is {entry.status!r} but names no verification. "
                f"A status that asserts a test must name the test."
            )
        if entry.status in STATUSES_WITHOUT_TESTS and has_tests:
            raise RegistryError(
                f"entry {entry.id!r} is {entry.status!r} but names {len(entry.verification)} "
                f"test(s). Use 'partial' if some verification already exists."
            )
        if entry.status in STATUSES_WITH_PHASE and not entry.planned_in:
            raise RegistryError(
                f"entry {entry.id!r} is {entry.status!r} but names no planned_in phase. "
                f"Unfinished work needs an owner."
            )
        if entry.status not in STATUSES_WITH_PHASE and entry.planned_in:
            raise RegistryError(
                f"entry {entry.id!r} is {entry.status!r} yet still points at "
                f"{entry.planned_in}. Clear planned_in when the work is done."
            )
        if entry.planned_in and entry.planned_in not in phases:
            raise RegistryError(
                f"entry {entry.id!r} is planned into {entry.planned_in!r}, which is not a "
                f"declared phase"
            )
        if (entry.status == "accepted_gap") != (entry.waiver is not None):
            raise RegistryError(
                f"entry {entry.id!r}: a waiver and status 'accepted_gap' go together. "
                f"status={entry.status!r}, waiver={'set' if entry.waiver else 'absent'}"
            )
        if entry.is_critical and entry.status == "accepted_gap":
            raise RegistryError(
                f"entry {entry.id!r} is critical and cannot be waived. Downgrade the "
                f"criticality honestly, or verify it."
            )


def _check_filesystem(entries: list[RegistryEntry], root: Path) -> None:
    """Named tests and owning modules must exist. This is the anti-fiction check."""
    for entry in entries:
        for ver in entry.verification:
            if not (root / ver.path).exists():
                raise RegistryError(
                    f"entry {entry.id!r} claims verification by {ver.path!r}, which is not "
                    f"on disk. The registry must describe the tree as it is."
                )
        for module in entry.owning_modules:
            if not (root / module).exists():
                raise RegistryError(
                    f"entry {entry.id!r} names owning module {module!r}, which is not on disk"
                )


def _check_graph(entries: list[RegistryEntry]) -> None:
    """depends_on resolves, and the dependency graph is acyclic."""
    id_set = {e.id for e in entries}
    for entry in entries:
        missing = [d for d in entry.depends_on if d not in id_set]
        if missing:
            raise RegistryError(
                f"entry {entry.id!r} depends on unknown entries: {', '.join(missing)}"
            )

    graph = {e.id: list(e.depends_on) for e in entries}
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(graph, WHITE)
    path: list[str] = []

    def visit(node: str) -> None:
        colour[node] = GREY
        path.append(node)
        for child in graph.get(node, []):
            if colour.get(child) == GREY:
                cycle = path[path.index(child) :] + [child]
                raise RegistryError(f"dependency cycle: {' -> '.join(cycle)}")
            if colour.get(child) == WHITE:
                visit(child)
        path.pop()
        colour[node] = BLACK

    for node in graph:
        if colour[node] == WHITE:
            visit(node)


def _check_semantics(raw: dict[str, Any], entries: Iterable[RegistryEntry], root: Path) -> None:
    ordered = list(entries)
    # Vocabularies first: an undeclared `kind` should be reported as an
    # undeclared kind, not as a prefix mismatch against a word that means
    # nothing here.
    _check_vocabularies(raw, ordered)
    _check_identity(ordered)
    _check_status_contract(raw, ordered)
    _check_filesystem(ordered, root)
    _check_graph(ordered)


def load_registry(path: Path | None = None, root: Path | None = None) -> QualityRegistry:
    """
    Read, validate and index the registry.

    Raises RegistryError on anything wrong: a missing file, malformed JSON, a
    schema violation, or a cross-entry inconsistency. There is no partial-load
    mode -- a half-valid governance file reads exactly like a valid one at the
    call site, which is the whole problem.

    ``root`` overrides the directory that test and module paths are resolved
    against; it exists so the loader's own tests can point at a fixture tree.
    """
    target = path or REGISTRY_PATH
    base = root or PROJECT_ROOT
    try:
        with target.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError as exc:
        raise RegistryError(f"quality registry not found at {target}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{target.name} is not valid JSON: {exc}") from exc

    _validate_against_schema(raw)
    entries = tuple(RegistryEntry.from_dict(e) for e in raw["entries"])
    _check_semantics(raw, entries, base)

    return QualityRegistry(
        registry_version=raw["registry_version"],
        kind_definitions=dict(raw["kind_definitions"]),
        status_definitions=dict(raw["status_definitions"]),
        criticality_definitions=dict(raw["criticality_definitions"]),
        test_types=dict(raw["test_types"]),
        subsystems=tuple(raw["subsystems"]),
        phases=dict(raw["phases"]),
        entries=entries,
        _by_id={e.id: e for e in entries},
    )


@lru_cache(maxsize=1)
def default_registry() -> QualityRegistry:
    """
    The process-wide registry.

    Cached because validation stats every named file and walks the dependency
    graph, and the file does not change while a process runs. Call
    ``default_registry.cache_clear()`` in a test that edits the file.
    """
    return load_registry()
