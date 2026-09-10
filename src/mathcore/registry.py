"""
Loader and query surface for ``config/math_registry.json``.

The registry answers three questions that would otherwise be re-litigated in
every session:

  1. Does this mathematical object actually do anything here, or is it
     folklore someone repeated confidently?
  2. Which module owns it, so a second implementation is not started?
  3. What breaks if it is used wrongly?

Validation is strict and happens at load time. A registry that has drifted --
a dangling ``depends_on``, two owners for one entry, a component module with
no owner, a folklore entry wired into a live strategy -- is a governance
failure, and the right moment to find out is at import, in CI, not when a
signal reaches an order router.

The file is read-only at runtime. Nothing here writes to it: it is edited by a
human or by a session doing registry work, and reviewed as a diff.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = PROJECT_ROOT / "config" / "math_registry.json"
SCHEMA_PATH = PROJECT_ROOT / "config" / "math_registry.schema.json"

#: Verdicts whose entries must never reach a live trading decision without
#: passing the validation gate described in their own registry entry.
GATED_VERDICTS = frozenset({"folklore"})

#: Statuses that assert code exists. Tests check that the claim is true.
IMPLEMENTED_STATUSES = frozenset({"implemented"})


class RegistryError(RuntimeError):
    """Raised when the registry is malformed, inconsistent or self-contradictory."""


@dataclass(frozen=True)
class WiringPoint:
    """One place in the tree that owns or consumes a registry entry."""

    module: str
    kind: str
    note: str = ""

    @property
    def is_owner(self) -> bool:
        return self.kind == "owner"

    @property
    def is_component(self) -> bool:
        """
        Whether this module implements part of a multi-module object.

        ``component`` exists for the one shape ``owner`` cannot express: an
        object that is genuinely one concept with more than one implementation.
        ``finite-fields`` is it -- GF(p) and GF(2^n) are the same algebraic
        object over different characteristics, and neither module implements
        the other's half. Listing both as owners would trip the one-owner rule,
        whose purpose is to catch an object accidentally implemented twice;
        listing only one would be a false claim about the other.

        A component is held to the same existence check as an owner. The kind
        buys an entry a second module, not a weaker standard.
        """
        return self.kind == "component"


@dataclass(frozen=True)
class RegistryEntry:
    """
    One mathematical object and this project's position on it.

    Frozen because a verdict that can be mutated at runtime is not a governance
    control. To change a verdict, edit the JSON and review the diff.
    """

    id: str
    name: str
    domain: str
    verdict: str
    role: str
    used_by: tuple[str, ...]
    repo_relevance: str
    status: str
    wiring: tuple[WiringPoint, ...] = ()
    depends_on: tuple[str, ...] = ()
    risk_if_misused: str = ""
    references: tuple[str, ...] = ()
    notes: str = ""

    @property
    def is_gated(self) -> bool:
        """True when this entry may not become a live signal unaided."""
        return self.verdict in GATED_VERDICTS

    @property
    def owners(self) -> tuple[WiringPoint, ...]:
        return tuple(w for w in self.wiring if w.is_owner)

    @property
    def components(self) -> tuple[WiringPoint, ...]:
        return tuple(w for w in self.wiring if w.is_component)

    @property
    def implementing_modules(self) -> tuple[WiringPoint, ...]:
        """Every module that implements this entry: its owner and any components."""
        return self.owners + self.components

    @property
    def consumers(self) -> tuple[WiringPoint, ...]:
        return tuple(w for w in self.wiring if w.kind == "consumer")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RegistryEntry:
        return cls(
            id=raw["id"],
            name=raw["name"],
            domain=raw["domain"],
            verdict=raw["verdict"],
            role=raw["role"],
            used_by=tuple(raw["used_by"]),
            repo_relevance=raw["repo_relevance"],
            status=raw["status"],
            wiring=tuple(
                WiringPoint(module=w["module"], kind=w["kind"], note=w.get("note", ""))
                for w in raw.get("wiring", [])
            ),
            depends_on=tuple(raw.get("depends_on", [])),
            risk_if_misused=raw.get("risk_if_misused", ""),
            references=tuple(raw.get("references", [])),
            notes=raw.get("notes", ""),
        )


@dataclass(frozen=True)
class MathRegistry:
    """The whole registry, indexed and validated."""

    registry_version: str
    verdict_definitions: dict[str, str]
    domains: tuple[str, ...]
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

    def by_domain(self, domain: str) -> tuple[RegistryEntry, ...]:
        return tuple(e for e in self.entries if e.domain == domain)

    def by_verdict(self, verdict: str) -> tuple[RegistryEntry, ...]:
        return tuple(e for e in self.entries if e.verdict == verdict)

    def by_relevance(self, relevance: str) -> tuple[RegistryEntry, ...]:
        return tuple(e for e in self.entries if e.repo_relevance == relevance)

    def owned_by(self, module: str) -> tuple[RegistryEntry, ...]:
        """Entries whose owning module is exactly ``module``."""
        return tuple(e for e in self.entries if any(w.module == module for w in e.owners))

    def implemented_by(self, module: str) -> tuple[RegistryEntry, ...]:
        """
        Entries ``module`` implements, as owner or as a component.

        The question "what am I on the hook for if I change this file?" is this
        one, not :meth:`owned_by` -- a component module carries the same
        obligations as an owner and is just as much the thing under test.
        """
        return tuple(
            e for e in self.entries if any(w.module == module for w in e.implementing_modules)
        )

    def gated(self) -> tuple[RegistryEntry, ...]:
        """Entries that must not become live signals without validation."""
        return tuple(e for e in self.entries if e.is_gated)

    # -- graph -----------------------------------------------------------

    def dependencies_of(self, entry_id: str) -> tuple[RegistryEntry, ...]:
        """Transitive dependencies, deepest first, with cycles already ruled out."""
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


def _check_semantics(registry_raw: dict[str, Any], entries: Iterable[RegistryEntry]) -> None:
    """
    Checks the JSON Schema cannot express.

    Schema validation proves each entry is well shaped. These checks prove the
    entries are consistent with each other, which is where a registry actually
    rots: a renamed id leaves a dangling dependency, a copied entry leaves two
    owners, a folklore entry quietly acquires a strategy module.
    """
    entries = list(entries)
    ids = [e.id for e in entries]

    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise RegistryError(f"duplicate entry ids: {', '.join(duplicates)}")

    known_domains = set(registry_raw["domains"])
    for entry in entries:
        if entry.domain not in known_domains:
            raise RegistryError(
                f"entry {entry.id!r} uses domain {entry.domain!r}, which is not in "
                f"the declared domain vocabulary"
            )

    known_verdicts = set(registry_raw["verdict_definitions"])
    for entry in entries:
        if entry.verdict not in known_verdicts:
            raise RegistryError(
                f"entry {entry.id!r} uses verdict {entry.verdict!r}, which has no "
                f"definition in verdict_definitions"
            )

    id_set = set(ids)
    for entry in entries:
        missing = [d for d in entry.depends_on if d not in id_set]
        if missing:
            raise RegistryError(
                f"entry {entry.id!r} depends on unknown entries: {', '.join(missing)}"
            )

    _check_acyclic(entries)

    for entry in entries:
        if entry.status in IMPLEMENTED_STATUSES and not entry.owners:
            raise RegistryError(
                f"entry {entry.id!r} is marked implemented but names no owning module"
            )
        if len(entry.owners) > 1 and entry.status in IMPLEMENTED_STATUSES:
            owners = ", ".join(w.module for w in entry.owners)
            raise RegistryError(
                f"entry {entry.id!r} claims several owners ({owners}); an implemented "
                f"entry has exactly one, or it has been implemented twice"
            )

    for entry in entries:
        if entry.components and not entry.owners:
            raise RegistryError(
                f"entry {entry.id!r} lists component modules but no owner; a component "
                f"is part of an implementation, not a substitute for one"
            )
        for module in entry.implementing_modules:
            if entry.status not in IMPLEMENTED_STATUSES:
                continue
            if not (PROJECT_ROOT / module.module).exists():
                raise RegistryError(
                    f"entry {entry.id!r} is marked implemented but its {module.kind} "
                    f"module {module.module!r} does not exist. The registry must "
                    f"describe the tree as it is, not as it is planned to be."
                )

    for entry in entries:
        if entry.is_gated and entry.status == "implemented":
            raise RegistryError(
                f"entry {entry.id!r} is folklore and cannot be marked implemented. "
                f"If it is being used, it must pass the validation gate first."
            )


def _check_acyclic(entries: list[RegistryEntry]) -> None:
    """Depth-first cycle detection over depends_on, reporting the actual cycle."""
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


def load_registry(path: Path | None = None) -> MathRegistry:
    """
    Read, validate and index the registry.

    Raises RegistryError on anything wrong: a missing file, malformed JSON, a
    schema violation or a cross-entry inconsistency. There is no partial-load
    mode, because a half-valid governance file is indistinguishable from a
    valid one at the call site.
    """
    target = path or REGISTRY_PATH
    try:
        with target.open(encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError as exc:
        raise RegistryError(f"registry not found at {target}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{target.name} is not valid JSON: {exc}") from exc

    _validate_against_schema(raw)
    entries = tuple(RegistryEntry.from_dict(e) for e in raw["entries"])
    _check_semantics(raw, entries)

    return MathRegistry(
        registry_version=raw["registry_version"],
        verdict_definitions=dict(raw["verdict_definitions"]),
        domains=tuple(raw["domains"]),
        entries=entries,
        _by_id={e.id: e for e in entries},
    )


@lru_cache(maxsize=1)
def default_registry() -> MathRegistry:
    """
    The process-wide registry.

    Cached because validation walks the whole dependency graph and the file
    does not change while a process runs. Call ``default_registry.cache_clear()``
    in a test that edits the file.
    """
    return load_registry()
