"""
Tests for the quality/security requirements registry.

Two halves, and both matter. The first is the loader's own logic: a registry
that accepts a broken entry is worse than no registry, because the traceability
matrix generated from it is then trusted and wrong. The second is the real
``config/quality_registry.json``, which must load, must stay honest about what
is on disk, and must not quietly acquire a critical requirement that nobody
owns.

The fixtures build small registries on a temporary tree so the failure cases can
be provoked without corrupting the real file.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from src.quality.registry import (
    DEFAULT_KIND,
    PREFIX_KINDS,
    QualityRegistry,
    RegistryEntry,
    RegistryError,
    Verification,
    default_registry,
    load_registry,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Fixture registry construction
# ---------------------------------------------------------------------------

BASE_VOCAB = {
    "registry_version": "9.9.9",
    "kind_definitions": {
        "requirement": "A property the system must have, stated testably.",
        "invariant": "A trading invariant that holds on every path, always.",
        "regression": "A defect that reached a running system once.",
        "security_regression": "A security weakness that was once possible.",
    },
    "status_definitions": {
        "verified": "A named test exists on disk and covers the statement.",
        "partial": "Some verification exists; planned_in finishes the job.",
        "planned": "No test yet; planned_in names the PR that adds one.",
        "accepted_gap": "A deliberate, dated, attributed decision not to verify.",
    },
    "criticality_definitions": {
        "critical": "Violation can lose money or leak a credential.",
        "high": "Violation degrades a safety control without moving funds.",
        "medium": "Violation costs correctness with a bounded blast radius.",
        "low": "Violation is a hygiene concern.",
    },
    "test_types": {
        "unit": "Does this function work?",
        "risk": "Can an unsafe trade pass?",
        "security": "Can an attacker violate a security property?",
    },
    "subsystems": ["risk", "execution", "governance"],
    "phases": {"PR-001": "Foundation", "PR-002": "Risk invariants"},
}


def entry(**overrides) -> dict:
    """A minimal valid entry, overridable field by field."""
    base = {
        "id": "RISK-001",
        "kind": "requirement",
        "title": "A requirement with a title",
        "statement": "The system must do the thing, checkably and on every path.",
        "criticality": "high",
        "subsystem": "risk",
        "status": "planned",
        "source": "QE-5",
        "planned_in": "PR-002",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def write_registry(tmp_path: Path, entries: list[dict], **vocab_overrides) -> Path:
    """Write a registry file into ``tmp_path`` and return its path."""
    raw = dict(BASE_VOCAB)
    raw.update(vocab_overrides)
    raw["entries"] = entries
    target = tmp_path / "quality_registry.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    return target


def load(tmp_path: Path, entries: list[dict], **vocab_overrides) -> QualityRegistry:
    path = write_registry(tmp_path, entries, **vocab_overrides)
    return load_registry(path=path, root=tmp_path)


def expect_error(tmp_path: Path, entries: list[dict], fragment: str, **vocab) -> None:
    with pytest.raises(RegistryError) as excinfo:
        load(tmp_path, entries, **vocab)
    assert fragment in str(excinfo.value)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A temporary tree with one real test file and one real module."""
    (tmp_path / "tests" / "risk").mkdir(parents=True)
    (tmp_path / "tests" / "risk" / "test_limits.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "risk").mkdir(parents=True)
    (tmp_path / "src" / "risk" / "gates.py").write_text("", encoding="utf-8")
    return tmp_path


VERIFIED = {
    "status": "verified",
    "planned_in": None,
    "verification": [{"test": "tests/risk/test_limits.py", "test_type": "risk"}],
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class TestLoading:
    def test_a_minimal_registry_loads(self, tree):
        registry = load(tree, [entry()])
        assert len(registry) == 1
        assert registry.registry_version == "9.9.9"

    def test_missing_file_is_an_error(self, tmp_path):
        with pytest.raises(RegistryError, match="not found"):
            load_registry(path=tmp_path / "nope.json", root=tmp_path)

    def test_malformed_json_is_an_error(self, tmp_path):
        target = tmp_path / "broken.json"
        target.write_text("{not json", encoding="utf-8")
        with pytest.raises(RegistryError, match="not valid JSON"):
            load_registry(path=target, root=tmp_path)

    def test_schema_violation_names_the_location(self, tmp_path):
        # A statement below the minimum length: the schema, not the semantics,
        # is what should catch this.
        expect_error(tmp_path, [entry(statement="too short")], "fails its schema at")

    def test_an_empty_entry_list_is_refused_by_the_schema(self, tmp_path):
        expect_error(tmp_path, [], "fails its schema at")


# ---------------------------------------------------------------------------
# Identity: ids and kinds
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_duplicate_ids_are_refused(self, tree):
        expect_error(tree, [entry(), entry()], "duplicate entry ids: RISK-001")

    @pytest.mark.parametrize(
        ("entry_id", "kind"),
        [("INV-001", "invariant"), ("REG-0001", "regression"), ("SEC-0001", "security_regression")],
    )
    def test_each_prefix_binds_its_kind(self, tree, entry_id, kind):
        registry = load(tree, [entry(id=entry_id, kind=kind)])
        assert registry.get(entry_id).kind == kind

    def test_a_mistyped_kind_cannot_hide_an_invariant(self, tree):
        expect_error(
            tree,
            [entry(id="INV-001", kind="requirement")],
            "its id prefix means 'invariant'",
        )

    def test_a_three_digit_sec_id_is_not_a_security_regression(self, tree):
        # SEC-001 matches the requirement id shape, but the SEC prefix means
        # security_regression -- so the pair is contradictory and must fail.
        expect_error(tree, [entry(id="SEC-001", kind="requirement")], "id prefix means")

    def test_prefix_table_and_default_agree_with_the_dataclass(self):
        assert PREFIX_KINDS["INV"] == "invariant"
        assert DEFAULT_KIND == "requirement"


# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------


class TestVocabularies:
    @pytest.mark.parametrize(
        ("field", "value", "source"),
        [
            ("kind", "guideline", "kind_definitions"),
            ("status", "probably_fine", "status_definitions"),
            ("criticality", "catastrophic", "criticality_definitions"),
            ("subsystem", "frontend", "subsystems"),
        ],
    )
    def test_an_undeclared_value_is_refused(self, tree, field, value, source):
        # kind must still match the prefix rule, so use a fresh prefix for it.
        overrides = {field: value}
        expect_error(tree, [entry(**overrides)], f"not declared in {source}")

    def test_an_undeclared_test_type_is_refused(self, tree):
        expect_error(
            tree,
            [
                entry(
                    status="verified",
                    planned_in=None,
                    verification=[{"test": "tests/risk/test_limits.py", "test_type": "vibes"}],
                )
            ],
            "not in the declared test taxonomy",
        )


# ---------------------------------------------------------------------------
# The status contract
# ---------------------------------------------------------------------------


class TestStatusContract:
    def test_verified_must_name_a_test(self, tree):
        expect_error(
            tree,
            [entry(status="verified", planned_in=None)],
            "names no verification",
        )

    def test_partial_must_name_a_test(self, tree):
        expect_error(tree, [entry(status="partial")], "names no verification")

    def test_planned_must_not_name_a_test(self, tree):
        expect_error(
            tree,
            [entry(verification=[{"test": "tests/risk/test_limits.py", "test_type": "risk"}])],
            "Use 'partial'",
        )

    def test_planned_must_name_a_phase(self, tree):
        expect_error(tree, [entry(planned_in=None)], "names no planned_in phase")

    def test_partial_must_name_a_phase(self, tree):
        expect_error(
            tree,
            [
                entry(
                    status="partial",
                    planned_in=None,
                    verification=[{"test": "tests/risk/test_limits.py", "test_type": "risk"}],
                )
            ],
            "names no planned_in phase",
        )

    def test_verified_must_not_still_point_at_a_phase(self, tree):
        expect_error(
            tree,
            [
                entry(
                    status="verified",
                    planned_in="PR-002",
                    verification=[{"test": "tests/risk/test_limits.py", "test_type": "risk"}],
                )
            ],
            "Clear planned_in when the work is done",
        )

    def test_an_undeclared_phase_is_refused(self, tree):
        expect_error(tree, [entry(planned_in="PR-099")], "not a declared phase")

    def test_a_waiver_requires_the_accepted_gap_status(self, tree):
        expect_error(
            tree,
            [
                entry(
                    waiver={
                        "reason": "This is deliberately not verified for a stated reason.",
                        "accepted_by": "operator",
                        "review_by": "2027-01-01",
                    }
                )
            ],
            "go together",
        )

    def test_accepted_gap_requires_a_waiver(self, tree):
        expect_error(tree, [entry(status="accepted_gap", planned_in=None)], "go together")

    def test_a_critical_entry_cannot_be_waived(self, tree):
        expect_error(
            tree,
            [
                entry(
                    criticality="critical",
                    status="accepted_gap",
                    planned_in=None,
                    waiver={
                        "reason": "We would rather not test this one, honestly speaking.",
                        "accepted_by": "operator",
                        "review_by": "2027-01-01",
                    },
                )
            ],
            "cannot be waived",
        )

    def test_a_non_critical_accepted_gap_loads(self, tree):
        registry = load(
            tree,
            [
                entry(
                    criticality="low",
                    status="accepted_gap",
                    planned_in=None,
                    waiver={
                        "reason": "Covered by a manual quarterly review instead of a test.",
                        "accepted_by": "operator",
                        "review_by": "2027-01-01",
                    },
                )
            ],
        )
        waiver = registry.get("RISK-001").waiver
        assert waiver.accepted_by == "operator"
        assert waiver.review_by == "2027-01-01"
        assert waiver.reason.startswith("Covered by")

    def test_a_waiver_past_its_review_date_is_reported(self, tree):
        registry = load(
            tree,
            [
                entry(
                    criticality="low",
                    status="accepted_gap",
                    planned_in=None,
                    waiver={
                        "reason": "Covered by a manual quarterly review instead of a test.",
                        "accepted_by": "operator",
                        "review_by": "2020-01-01",
                    },
                )
            ],
        )
        assert [e.id for e in registry.expired_waivers(date(2021, 1, 1))] == ["RISK-001"]
        assert registry.expired_waivers(date(2019, 1, 1)) == ()

    def test_an_entry_without_a_waiver_never_expires(self, tree):
        registry = load(tree, [entry()])
        assert registry.expired_waivers(date(2099, 1, 1)) == ()


# ---------------------------------------------------------------------------
# Filesystem honesty
# ---------------------------------------------------------------------------


class TestFilesystemHonesty:
    def test_a_named_test_that_is_not_on_disk_is_refused(self, tree):
        expect_error(
            tree,
            [
                entry(
                    status="verified",
                    planned_in=None,
                    verification=[{"test": "tests/risk/test_ghost.py", "test_type": "risk"}],
                )
            ],
            "which is not on disk",
        )

    def test_a_named_owning_module_that_is_not_on_disk_is_refused(self, tree):
        expect_error(tree, [entry(owning_modules=["src/risk/imaginary.py"])], "not on disk")

    def test_a_node_selector_resolves_to_the_file(self, tree):
        registry = load(
            tree,
            [
                entry(
                    status="verified",
                    planned_in=None,
                    verification=[
                        {
                            "test": "tests/risk/test_limits.py::TestCeiling::test_at_limit",
                            "test_type": "risk",
                        }
                    ],
                )
            ],
        )
        ver = registry.get("RISK-001").verification[0]
        assert ver.path == "tests/risk/test_limits.py"
        assert ver.node == "TestCeiling::test_at_limit"

    def test_a_plain_path_has_no_node(self, tree):
        ver = Verification(test="tests/risk/test_limits.py", test_type="risk")
        assert ver.node == ""


# ---------------------------------------------------------------------------
# The dependency graph
# ---------------------------------------------------------------------------


class TestGraph:
    def test_a_dangling_dependency_is_refused(self, tree):
        expect_error(tree, [entry(depends_on=["RISK-999"])], "depends on unknown entries")

    def test_a_cycle_is_reported_with_its_path(self, tree):
        expect_error(
            tree,
            [
                entry(id="RISK-001", depends_on=["RISK-002"]),
                entry(id="RISK-002", depends_on=["RISK-001"]),
            ],
            "dependency cycle:",
        )

    def test_a_self_cycle_is_refused(self, tree):
        expect_error(tree, [entry(depends_on=["RISK-001"])], "dependency cycle:")

    def test_transitive_dependencies_resolve(self, tree):
        registry = load(
            tree,
            [
                entry(id="RISK-001", depends_on=["RISK-002"]),
                entry(id="RISK-002", depends_on=["RISK-003"]),
                entry(id="RISK-003"),
            ],
        )
        ids = {e.id for e in registry.dependencies_of("RISK-001")}
        assert ids == {"RISK-002", "RISK-003"}

    def test_a_diamond_is_walked_once(self, tree):
        registry = load(
            tree,
            [
                entry(id="RISK-001", depends_on=["RISK-002", "RISK-003"]),
                entry(id="RISK-002", depends_on=["RISK-004"]),
                entry(id="RISK-003", depends_on=["RISK-004"]),
                entry(id="RISK-004"),
            ],
        )
        found = [e.id for e in registry.dependencies_of("RISK-001")]
        assert sorted(found) == ["RISK-002", "RISK-003", "RISK-004"]

    def test_dependents_are_found(self, tree):
        registry = load(
            tree,
            [entry(id="RISK-001", depends_on=["RISK-002"]), entry(id="RISK-002")],
        )
        assert [e.id for e in registry.dependents_of("RISK-002")] == ["RISK-001"]

    def test_dependents_of_an_unknown_entry_raises(self, tree):
        registry = load(tree, [entry()])
        with pytest.raises(RegistryError, match="no registry entry"):
            registry.dependents_of("RISK-404")


# ---------------------------------------------------------------------------
# Query surface
# ---------------------------------------------------------------------------


class TestQuerySurface:
    @pytest.fixture
    def registry(self, tree) -> QualityRegistry:
        return load(
            tree,
            [
                entry(id="RISK-001", **VERIFIED),
                entry(id="EXEC-001", subsystem="execution", criticality="critical"),
                entry(
                    id="INV-001",
                    kind="invariant",
                    subsystem="risk",
                    status="partial",
                    verification=[{"test": "tests/risk/test_limits.py", "test_type": "risk"}],
                ),
            ],
        )

    def test_iteration_and_length(self, registry):
        assert len(registry) == 3
        assert len(list(registry)) == 3

    def test_membership(self, registry):
        assert "RISK-001" in registry
        assert "RISK-404" not in registry

    def test_get_returns_the_entry(self, registry):
        assert registry.get("EXEC-001").subsystem == "execution"

    def test_get_suggests_near_matches(self, registry):
        with pytest.raises(RegistryError, match="Did you mean"):
            registry.get("RISK-0011")

    def test_get_without_near_matches_still_raises(self, registry):
        with pytest.raises(RegistryError) as excinfo:
            registry.get("ZZZ-999")
        assert "Did you mean" not in str(excinfo.value)

    def test_by_kind(self, registry):
        assert [e.id for e in registry.by_kind("invariant")] == ["INV-001"]

    def test_by_status(self, registry):
        assert [e.id for e in registry.by_status("verified")] == ["RISK-001"]

    def test_by_subsystem(self, registry):
        assert {e.id for e in registry.by_subsystem("risk")} == {"RISK-001", "INV-001"}

    def test_by_criticality(self, registry):
        assert [e.id for e in registry.by_criticality("critical")] == ["EXEC-001"]

    def test_by_phase(self, registry):
        assert {e.id for e in registry.by_phase("PR-002")} == {"EXEC-001", "INV-001"}

    def test_by_unknown_phase_raises(self, registry):
        with pytest.raises(RegistryError, match="unknown phase"):
            registry.by_phase("PR-099")

    def test_outstanding_excludes_verified(self, registry):
        assert [e.id for e in registry.outstanding()] == ["EXEC-001", "INV-001"]

    def test_tests_for_deduplicates(self, tree):
        registry = load(
            tree,
            [
                entry(
                    status="verified",
                    planned_in=None,
                    verification=[
                        {"test": "tests/risk/test_limits.py::test_a", "test_type": "risk"},
                        {"test": "tests/risk/test_limits.py::test_b", "test_type": "risk"},
                    ],
                )
            ],
        )
        assert registry.tests_for("RISK-001") == ("tests/risk/test_limits.py",)

    def test_entries_for_test_is_the_reverse_index(self, registry):
        found = registry.entries_for_test("tests/risk/test_limits.py")
        assert {e.id for e in found} == {"RISK-001", "INV-001"}

    def test_coverage_by_status_counts_every_declared_status(self, registry):
        counts = registry.coverage_by_status()
        assert counts == {"verified": 1, "partial": 1, "planned": 1, "accepted_gap": 0}

    def test_entry_predicates(self, registry):
        assert registry.get("RISK-001").is_verified
        assert registry.get("EXEC-001").is_critical
        assert not registry.get("EXEC-001").is_verified


# ---------------------------------------------------------------------------
# The dataclasses themselves
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_entries_are_frozen(self, tree):
        registry = load(tree, [entry()])
        with pytest.raises(AttributeError):
            registry.get("RISK-001").status = "verified"

    def test_from_dict_carries_the_optional_fields(self, tree):
        registry = load(
            tree,
            [
                entry(
                    owning_modules=["src/risk/gates.py"],
                    failure_mode="Money leaves.",
                    notes="A note.",
                )
            ],
        )
        found = registry.get("RISK-001")
        assert found.owning_modules == ("src/risk/gates.py",)
        assert found.failure_mode == "Money leaves."
        assert found.notes == "A note."

    def test_expected_kind_of_an_unprefixed_id_is_the_default(self):
        made = RegistryEntry(
            id="RISK-001",
            kind="requirement",
            title="t",
            statement="s",
            criticality="high",
            subsystem="risk",
            status="planned",
            source="QE-1",
        )
        assert made.expected_kind == "requirement"


# ---------------------------------------------------------------------------
# The real registry
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", name="real_registry")
def _real_registry() -> QualityRegistry:
    return load_registry()


class TestTheRealRegistry:
    @pytest.fixture
    def registry(self, real_registry) -> QualityRegistry:
        return real_registry

    def test_it_loads(self, registry):
        assert len(registry) > 0

    def test_default_registry_is_cached(self):
        assert default_registry() is default_registry()

    def test_every_named_test_exists(self, registry):
        # Redundant with the loader, and deliberately so: this is the assertion
        # a reader of the suite is looking for, and it names the offender.
        missing = [
            (e.id, path)
            for e in registry
            for path in e.test_paths
            if not (PROJECT_ROOT / path).exists()
        ]
        assert not missing

    def test_every_owning_module_exists(self, registry):
        missing = [
            (e.id, m) for e in registry for m in e.owning_modules if not (PROJECT_ROOT / m).exists()
        ]
        assert not missing

    def test_the_ten_trading_invariants_are_all_present(self, registry):
        found = {e.id for e in registry.by_kind("invariant")}
        assert found == {f"INV-{n:03d}" for n in range(1, 11)}

    def test_every_invariant_is_critical(self, registry):
        # An invariant that is not critical is not an invariant.
        assert all(e.is_critical for e in registry.by_kind("invariant"))

    def test_no_critical_entry_is_waived(self, registry):
        assert not [e.id for e in registry.by_criticality("critical") if e.status == "accepted_gap"]

    def test_every_unfinished_entry_names_a_phase(self, registry):
        homeless = [e.id for e in registry.outstanding() if not (e.planned_in or e.waiver)]
        assert not homeless

    def test_every_phase_has_work_or_is_finished(self, registry):
        # A declared phase with nothing scheduled into it is either done or a
        # typo. PR-001 is this PR, so it is allowed to be empty.
        empty = [p for p in registry.phases if p != "PR-001" and not registry.by_phase(p)]
        assert not empty

    def test_every_entry_states_a_failure_mode(self, registry):
        # "What goes wrong" is the field that makes a requirement arguable.
        assert not [e.id for e in registry if not e.failure_mode]

    def test_no_waiver_has_expired(self, registry):
        # A waiver past its review date is a risk nobody has re-accepted.
        today = datetime.now(UTC).date()
        assert not [e.id for e in registry.expired_waivers(today)]

    def test_every_entry_cites_the_source_document(self, registry):
        assert all(e.source.startswith("QE-") for e in registry)

    def test_each_declared_subsystem_is_used(self, registry):
        # A vocabulary entry nothing uses is either a gap in the registry or a
        # word left behind by a rename. Both are worth failing on.
        unused = [s for s in registry.subsystems if not registry.by_subsystem(s)]
        assert not unused
