"""
The registry schema enforces what it claims to.

A JSON Schema that accepts everything is the easiest thing in the world to
write, and it looks identical in review to one that enforces a contract. The
only way to tell them apart is to hand it documents that *should* fail and
check that they do.

So every rule in `config/quality_registry.schema.json` appears below twice:
once as the shipped registry passing, and once as a minimal mutation that must
be rejected. The mutations are deliberately the realistic ones — a status
flipped to `verified` without adding a test, a `planned_in` left behind after
an entry shipped, a critical requirement quietly waived — because those are
what a hurried edit actually produces.

The rules that are *not* here are the ones JSON Schema cannot express: whether
a named test exists on disk, whether `depends_on` resolves, and whether the
dependency graph is acyclic. Those live in `src/quality/registry.py` and are
covered by `tests/quality/test_quality_registry.py`. Keeping the boundary
visible matters — a reader has to be able to tell which half is enforcing
what, or they will assume the schema covers more than it does.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

jsonschema = pytest.importorskip("jsonschema")
from jsonschema import Draft7Validator  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO / "config" / "quality_registry.schema.json"
REGISTRY_PATH = REPO / "config" / "quality_registry.json"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator(schema) -> Draft7Validator:
    return Draft7Validator(schema)


@pytest.fixture(scope="module")
def registry() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def with_entry(registry: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """The shipped registry with `entry` substituted for its only entry."""
    document = copy.deepcopy(registry)
    document["entries"] = [entry]
    return document


def find(registry: dict[str, Any], status: str) -> dict[str, Any]:
    for entry in registry["entries"]:
        if entry["status"] == status:
            return copy.deepcopy(entry)
    pytest.skip(f"the registry currently has no {status!r} entry to mutate")


class TestTheSchemaItself:
    def test_it_is_a_valid_draft_07_schema(self, schema):
        # A schema with a typo in a keyword name is not an error -- unknown
        # keywords are ignored, so the rule silently does nothing. This is the
        # check that catches that.
        Draft7Validator.check_schema(schema)

    def test_every_conditional_rule_is_titled(self, schema):
        # The titles are what a failing validation prints. An untitled rule
        # produces an error message nobody can act on.
        for rule in schema["definitions"]["entry"]["allOf"]:
            assert rule.get("title"), f"untitled rule: {json.dumps(rule)[:80]}"

    def test_the_boundary_with_the_loader_is_documented(self, schema):
        # A reader has to be able to tell which half enforces what.
        description = schema["description"]
        assert "exists on disk" in description
        assert "acyclic" in description


class TestTheShippedRegistryPasses:
    def test_it_validates(self, validator, registry):
        errors = sorted(validator.iter_errors(registry), key=lambda e: list(e.path))
        rendered = [f"{list(e.path)}: {e.message}" for e in errors[:5]]
        assert not errors, "\n".join(rendered)

    def test_it_is_not_empty(self, registry):
        # A schema check over an empty entries array passes vacuously.
        assert len(registry["entries"]) >= 50


class TestStatusIsAClaimAboutEvidence:
    def test_verified_without_a_test_is_rejected(self, validator, registry):
        # The failure the whole registry exists to prevent: a requirement that
        # reports green because nobody wrote the test.
        entry = find(registry, "planned")
        entry["status"] = "verified"
        entry.pop("planned_in", None)
        assert not validator.is_valid(with_entry(registry, entry))

    def test_partial_without_a_test_is_rejected(self, validator, registry):
        entry = find(registry, "partial")
        entry["verification"] = []
        assert not validator.is_valid(with_entry(registry, entry))

    def test_planned_with_a_test_is_rejected(self, validator, registry):
        # Planned means no evidence yet. A planned entry carrying a
        # verification is a claim with nothing behind it.
        entry = find(registry, "planned")
        entry["verification"] = [{"test": "tests/quality/test_x.py", "test_type": "unit"}]
        assert not validator.is_valid(with_entry(registry, entry))

    def test_planned_without_a_phase_is_rejected(self, validator, registry):
        entry = find(registry, "planned")
        entry.pop("planned_in")
        assert not validator.is_valid(with_entry(registry, entry))

    def test_partial_without_a_phase_is_rejected(self, validator, registry):
        entry = find(registry, "partial")
        entry.pop("planned_in")
        assert not validator.is_valid(with_entry(registry, entry))

    def test_verified_with_a_leftover_phase_is_rejected(self, validator, registry):
        # planned_in left behind after an entry ships makes the "outstanding
        # work by phase" table lie.
        entry = find(registry, "partial")
        entry["status"] = "verified"
        assert not validator.is_valid(with_entry(registry, entry))

    def test_a_verified_entry_with_a_test_and_no_phase_passes(self, validator, registry):
        entry = find(registry, "partial")
        entry["status"] = "verified"
        entry.pop("planned_in")
        assert validator.is_valid(with_entry(registry, entry))


class TestGapsAreNeverSilent:
    def test_an_accepted_gap_without_a_waiver_is_rejected(self, validator, registry):
        entry = find(registry, "planned")
        entry["status"] = "accepted_gap"
        entry.pop("planned_in")
        entry["criticality"] = "low"
        assert not validator.is_valid(with_entry(registry, entry))

    def test_a_waiver_without_an_expiry_is_rejected(self, validator, registry):
        # A waiver with no review date is a permanent hole wearing a
        # temporary label.
        entry = find(registry, "planned")
        entry["status"] = "accepted_gap"
        entry.pop("planned_in")
        entry["criticality"] = "low"
        entry["waiver"] = {
            "reason": "accepted for now, see the ticket for the argument",
            "accepted_by": "operator",
        }
        assert not validator.is_valid(with_entry(registry, entry))

    def test_a_complete_waiver_passes(self, validator, registry):
        entry = find(registry, "planned")
        entry["status"] = "accepted_gap"
        entry.pop("planned_in")
        entry["criticality"] = "low"
        entry["waiver"] = {
            "reason": "the venue does not expose the field this requires yet",
            "accepted_by": "operator",
            "review_by": "2027-01-01",
        }
        assert validator.is_valid(with_entry(registry, entry))

    def test_a_critical_requirement_cannot_be_waived(self, validator, registry):
        # To waive it you must first argue, in the diff, that it is not
        # critical. That argument is the control.
        entry = find(registry, "planned")
        entry["criticality"] = "critical"
        entry["status"] = "accepted_gap"
        entry.pop("planned_in")
        entry["waiver"] = {
            "reason": "we would rather ship than finish this control",
            "accepted_by": "operator",
            "review_by": "2027-01-01",
        }
        assert not validator.is_valid(with_entry(registry, entry))


class TestTheIdPrefixAndTheKindAreOneFact:
    @pytest.mark.parametrize(
        "entry_id,wrong_kind",
        [
            ("INV-099", "requirement"),
            ("REG-9999", "invariant"),
            ("SEC-9999", "regression"),
            ("RISK-099", "invariant"),
        ],
    )
    def test_a_mismatch_is_rejected(self, validator, registry, entry_id, wrong_kind):
        entry = find(registry, "planned")
        entry["id"] = entry_id
        entry["kind"] = wrong_kind
        assert not validator.is_valid(with_entry(registry, entry))

    @pytest.mark.parametrize(
        "entry_id,kind",
        [
            ("INV-099", "invariant"),
            ("REG-9999", "regression"),
            ("SEC-9999", "security_regression"),
            ("RISK-099", "requirement"),
        ],
    )
    def test_the_matching_pair_passes(self, validator, registry, entry_id, kind):
        entry = find(registry, "planned")
        entry["id"] = entry_id
        entry["kind"] = kind
        assert validator.is_valid(with_entry(registry, entry))


class TestTheVocabulariesAreClosed:
    @pytest.mark.parametrize("field", ["kind", "status", "criticality"])
    def test_an_invented_value_is_rejected(self, validator, registry, field):
        # Free-text here is how "mostly_verified" appears and quietly counts
        # as done in somebody's dashboard.
        entry = find(registry, "planned")
        entry[field] = "mostly_fine"
        assert not validator.is_valid(with_entry(registry, entry))

    def test_an_unknown_top_level_key_is_rejected(self, validator, registry):
        document = copy.deepcopy(registry)
        document["coverage_percentage"] = 87
        assert not validator.is_valid(document)

    def test_an_unknown_entry_key_is_rejected(self, validator, registry):
        entry = find(registry, "planned")
        entry["mostly_done"] = True
        assert not validator.is_valid(with_entry(registry, entry))


class TestEveryEntryCanBeActedOn:
    def test_an_entry_with_no_failure_mode_is_rejected(self, validator, registry):
        # An entry nobody can describe the failure of is one nobody can size
        # the priority of.
        entry = find(registry, "planned")
        entry.pop("failure_mode", None)
        assert not validator.is_valid(with_entry(registry, entry))

    def test_a_slogan_statement_is_rejected(self, validator, registry):
        entry = find(registry, "planned")
        entry["statement"] = "Be secure."
        assert not validator.is_valid(with_entry(registry, entry))

    def test_a_test_path_outside_tests_is_rejected(self, validator, registry):
        entry = find(registry, "partial")
        entry["verification"] = [{"test": "src/api/main.py", "test_type": "unit"}]
        assert not validator.is_valid(with_entry(registry, entry))

    def test_a_source_that_names_no_section_is_rejected(self, validator, registry):
        entry = find(registry, "planned")
        entry["source"] = "the document"
        assert not validator.is_valid(with_entry(registry, entry))
