"""
Tests for scripts/check_static_invariants.py — and the gate that runs it.

The script was written, documented, and never executed: nothing in the repo
imported it and no workflow called it, so every invariant it declares was
unenforced. That is the same "fully built, zero callers" shape the script
itself exists to detect.

Running it from pytest is what makes it real. CI already runs pytest, so the
invariants are enforced on every push without a workflow change, and a
violation names the file and line rather than failing somewhere downstream.

The per-check tests below feed each check a synthetic tree containing the
exact defect it was written for, then the same tree with the defect removed.
A check that cannot fail is worse than no check, so both directions are
asserted.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_static_invariants.py"


def _load() -> ModuleType:
    """Import the script by path — scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("check_static_invariants", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def invariants() -> ModuleType:
    return _load()


@pytest.fixture
def fake_tree(invariants, tmp_path, monkeypatch):
    """
    Point the checks at a synthetic src/ + tests/ instead of the real repo.

    Returns a writer taking a path relative to the fake repo root.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(invariants, "REPO", tmp_path)
    monkeypatch.setattr(invariants, "SRC", tmp_path / "src")

    def write(relative: str, source: str) -> None:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")

    return write


# ---------------------------------------------------------------------------
# The real repository must satisfy every invariant
# ---------------------------------------------------------------------------


def test_repository_satisfies_every_invariant(invariants) -> None:
    """
    This is the gate. A failure here names the offending file and line.
    """
    violations: list[str] = []
    for label, check in invariants.CHECKS:
        violations.extend(f"[{label}] {problem}" for problem in check())
    assert not violations, "static invariant violations:\n" + "\n".join(violations)


def test_every_check_is_registered(invariants) -> None:
    """A check absent from CHECKS never runs — the defect the script is about."""
    defined = {
        name
        for name in dir(invariants)
        if name.startswith("check_") and callable(getattr(invariants, name))
    }
    registered = {check.__name__ for _, check in invariants.CHECKS}
    assert defined == registered, f"unregistered checks: {sorted(defined - registered)}"


# ---------------------------------------------------------------------------
# check_migration_versions
# ---------------------------------------------------------------------------


def _migrations(sqlite_versions: tuple[int, ...], pg_versions: tuple[int, ...]) -> tuple[str, str]:
    def render(name: str, versions: tuple[int, ...]) -> str:
        entries = "\n".join(f'    ({v}, "desc {v}", "SQL {v}"),' for v in versions)
        return f"from typing import Final\n\n{name}: Final[list] = [\n{entries}\n]\n"

    return render("_MIGRATIONS", sqlite_versions), render("_PG_MIGRATIONS", pg_versions)


def test_duplicate_migration_version_is_flagged(invariants, fake_tree) -> None:
    sqlite, pg = _migrations((1, 2, 2), (1, 2))
    fake_tree("src/data/storage.py", sqlite)
    fake_tree("src/data/timescale_storage.py", pg)
    problems = invariants.check_migration_versions()
    assert any("version 2 defined twice" in p for p in problems)


def test_backend_migration_drift_is_flagged(invariants, fake_tree) -> None:
    """A version in one backend and not the other is two schemas, one codebase."""
    sqlite, pg = _migrations((1, 2, 3), (1, 2))
    fake_tree("src/data/storage.py", sqlite)
    fake_tree("src/data/timescale_storage.py", pg)
    problems = invariants.check_migration_versions()
    assert any("migration 3 is in" in p and "but not" in p for p in problems)


def test_matching_migration_lists_pass(invariants, fake_tree) -> None:
    sqlite, pg = _migrations((1, 2, 3), (1, 2, 3))
    fake_tree("src/data/storage.py", sqlite)
    fake_tree("src/data/timescale_storage.py", pg)
    assert invariants.check_migration_versions() == []


def test_missing_migration_list_is_reported_not_silently_passed(invariants, fake_tree) -> None:
    """A renamed list must fail loudly rather than vacuously succeed."""
    fake_tree("src/data/storage.py", "RENAMED = []\n")
    fake_tree("src/data/timescale_storage.py", "RENAMED = []\n")
    problems = invariants.check_migration_versions()
    assert len(problems) == 2
    assert all("could not read" in p for p in problems)


# ---------------------------------------------------------------------------
# check_enum_members_exist
# ---------------------------------------------------------------------------


_ENUM_SRC = """
from enum import Enum


class DiscrepancyType(Enum):
    MISSING_LOCALLY = "missing_locally"
    MISSING_IN_REFERENCE = "missing_in_reference"

    @property
    def label(self) -> str:
        return self.value
"""


def test_renamed_enum_member_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/recovery.py", _ENUM_SRC)
    fake_tree(
        "tests/test_recovery.py",
        "from src.recovery import DiscrepancyType\n"
        "def test_it():\n"
        "    assert DiscrepancyType.MISSING_ON_EXCHANGE\n",
    )
    problems = invariants.check_enum_members_exist()
    assert any("MISSING_ON_EXCHANGE is not a member" in p for p in problems)


def test_existing_enum_member_passes(invariants, fake_tree) -> None:
    fake_tree("src/recovery.py", _ENUM_SRC)
    fake_tree(
        "tests/test_recovery.py",
        "from src.recovery import DiscrepancyType\n"
        "def test_it():\n"
        "    assert DiscrepancyType.MISSING_IN_REFERENCE\n",
    )
    assert invariants.check_enum_members_exist() == []


def test_lowercase_attribute_on_an_enum_is_not_flagged(invariants, fake_tree) -> None:
    """Enums may define methods and properties; only SHOUTY names are members."""
    fake_tree("src/recovery.py", _ENUM_SRC)
    fake_tree(
        "tests/test_recovery.py",
        "from src.recovery import DiscrepancyType\n"
        "def test_it():\n"
        "    assert DiscrepancyType.MISSING_LOCALLY.label\n",
    )
    assert invariants.check_enum_members_exist() == []


# ---------------------------------------------------------------------------
# check_keyword_arguments_match_signatures
# ---------------------------------------------------------------------------


_STRATEGY_SRC = """
class OptionsCarryStrategy:
    def __init__(self, max_capital_fraction=0.1, greeks_caps=None, cfg=None):
        self._caps = greeks_caps


def helper(alpha, *, beta=None):
    return alpha, beta


class Flexible:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
"""


def test_renamed_keyword_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/strategy.py", _STRATEGY_SRC)
    fake_tree(
        "tests/test_strategy.py",
        "from src.strategy import OptionsCarryStrategy\n"
        "def test_it():\n"
        "    OptionsCarryStrategy(caps=None)\n",
    )
    problems = invariants.check_keyword_arguments_match_signatures()
    assert any("has no parameter 'caps'" in p for p in problems)


def test_accepted_keywords_pass(invariants, fake_tree) -> None:
    fake_tree("src/strategy.py", _STRATEGY_SRC)
    fake_tree(
        "tests/test_strategy.py",
        "from src.strategy import OptionsCarryStrategy, helper\n"
        "def test_it():\n"
        "    OptionsCarryStrategy(greeks_caps=None, cfg=None)\n"
        "    helper(alpha=1, beta=2)\n",
    )
    assert invariants.check_keyword_arguments_match_signatures() == []


def test_kwargs_target_is_not_checked(invariants, fake_tree) -> None:
    """**kwargs accepts anything, so no conclusion can be drawn."""
    fake_tree("src/strategy.py", _STRATEGY_SRC)
    fake_tree(
        "tests/test_strategy.py",
        "from src.strategy import Flexible\ndef test_it():\n    Flexible(anything=1)\n",
    )
    assert invariants.check_keyword_arguments_match_signatures() == []


def test_name_defined_twice_with_different_signatures_is_skipped(invariants, fake_tree) -> None:
    """
    Resolution is by bare name, so an ambiguous name must be dropped rather
    than guessed at — a false positive here would block a correct push.
    """
    fake_tree("src/a.py", "def build(alpha=None):\n    return alpha\n")
    fake_tree("src/b.py", "def build(beta=None):\n    return beta\n")
    fake_tree("tests/test_build.py", "from src.b import build\ndef test_it():\n    build(beta=1)\n")
    assert invariants.check_keyword_arguments_match_signatures() == []


# ---------------------------------------------------------------------------
# check_dataclass_attributes_exist
# ---------------------------------------------------------------------------

_RESULT_SRC = """
from dataclasses import dataclass


@dataclass
class WorkerResult:
    horizon_id: int
    confidence: float

    def label(self) -> str:
        return str(self.horizon_id)


@dataclass
class Bag:
    items: list


@dataclass
class Derived(Bag):
    extra: int
"""


def test_renamed_dataclass_field_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/workers.py", _RESULT_SRC)
    fake_tree(
        "src/reader.py",
        "from src.workers import WorkerResult\n"
        "def read():\n"
        "    r = WorkerResult(horizon_id=1, confidence=0.5)\n"
        "    return r.horizon_idx\n",
    )
    problems = invariants.check_dataclass_attributes_exist()
    assert len(problems) == 1
    assert "WorkerResult has no attribute 'horizon_idx'" in problems[0]


def test_existing_field_and_method_pass(invariants, fake_tree) -> None:
    """Methods defined on the dataclass are reachable too, not just fields."""
    fake_tree("src/workers.py", _RESULT_SRC)
    fake_tree(
        "src/reader.py",
        "from src.workers import WorkerResult\n"
        "def read():\n"
        "    r = WorkerResult(horizon_id=1, confidence=0.5)\n"
        "    return r.horizon_id, r.confidence, r.label()\n",
    )
    assert invariants.check_dataclass_attributes_exist() == []


def test_field_reached_through_an_annotated_list_is_flagged(invariants, fake_tree) -> None:
    """
    The real defect took this shape: a list built up over a loop, then
    ``max()``-ed, then read. Nothing in between states the type but the
    annotation on the empty list.
    """
    fake_tree("src/workers.py", _RESULT_SRC)
    fake_tree(
        "src/reader.py",
        "from src.workers import WorkerResult\n"
        "def read(source):\n"
        "    results: list[WorkerResult] = []\n"
        "    for item in source:\n"
        "        results.append(item)\n"
        "    best = max(results, key=lambda r: r.confidence)\n"
        "    return best.horizon_idx\n",
    )
    problems = invariants.check_dataclass_attributes_exist()
    assert len(problems) == 1
    assert "has no attribute 'horizon_idx'" in problems[0]


def test_subclass_is_not_checked(invariants, fake_tree) -> None:
    """Inherited attributes are invisible to an AST pass — do not guess."""
    fake_tree("src/workers.py", _RESULT_SRC)
    fake_tree(
        "src/reader.py",
        "from src.workers import Derived\n"
        "def read():\n"
        "    d = Derived(items=[], extra=1)\n"
        "    return d.items\n",
    )
    assert invariants.check_dataclass_attributes_exist() == []


def test_rebound_name_is_dropped(invariants, fake_tree) -> None:
    """Once a name holds something else, nothing can be concluded about it."""
    fake_tree("src/workers.py", _RESULT_SRC)
    fake_tree(
        "src/reader.py",
        "from src.workers import WorkerResult\n"
        "def read(other):\n"
        "    r = WorkerResult(horizon_id=1, confidence=0.5)\n"
        "    r = other\n"
        "    return r.anything_at_all\n",
    )
    assert invariants.check_dataclass_attributes_exist() == []


def test_non_dataclass_is_not_checked(invariants, fake_tree) -> None:
    fake_tree("src/plain.py", "class Plain:\n    def __init__(self):\n        self.x = 1\n")
    fake_tree(
        "src/reader.py",
        "from src.plain import Plain\ndef read():\n    p = Plain()\n    return p.whatever\n",
    )
    assert invariants.check_dataclass_attributes_exist() == []
