"""
Tests for the mathematical foundations registry.

The registry is a governance artefact, so these tests check the properties
that make it trustworthy rather than the properties that make it parse: that
it cannot claim code which does not exist, that folklore cannot quietly become
a live signal, that its dependency graph resolves, and that the documentation
describing it has not drifted away from it.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from src.mathcore.registry import (
    REGISTRY_PATH,
    SCHEMA_PATH,
    MathRegistry,
    RegistryError,
    load_registry,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def registry() -> MathRegistry:
    return load_registry()


@pytest.fixture()
def raw() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


class TestLoads:
    def test_registry_loads_and_validates(self, registry):
        assert len(registry) > 0

    def test_schema_file_is_valid_draft7(self):
        import jsonschema

        jsonschema.Draft7Validator.check_schema(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))

    def test_registry_validates_against_its_own_schema(self, raw):
        import jsonschema

        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        errors = list(jsonschema.Draft7Validator(schema).iter_errors(raw))
        assert errors == [], errors[:1]

    def test_missing_file_raises_a_clear_error(self, tmp_path):
        with pytest.raises(RegistryError, match="not found"):
            load_registry(tmp_path / "absent.json")

    def test_malformed_json_raises_a_clear_error(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with pytest.raises(RegistryError, match="not valid JSON"):
            load_registry(bad)


class TestCoverage:
    """The registry must actually answer the question it was built to answer."""

    def test_every_declared_domain_has_at_least_one_entry(self, registry):
        for domain in registry.domains:
            assert registry.by_domain(domain), f"domain {domain} has no entries"

    def test_every_verdict_is_exercised(self, registry):
        for verdict in registry.verdict_definitions:
            assert registry.by_verdict(verdict), f"verdict {verdict} is never used"

    def test_the_load_bearing_core_is_present(self, registry):
        # If any of these went missing the registry would be describing some
        # other project's cryptography.
        for entry_id in [
            "finite-fields",
            "elliptic-curves",
            "cyclic-groups-dlp",
            "ntt",
            "prime-factorisation-rsa",
            "birthday-bound",
            "hidden-number-problem",
            "rfc6979-deterministic-nonces",
        ]:
            assert entry_id in registry

    def test_the_folklore_claims_are_refuted_not_omitted(self, registry):
        for entry_id in [
            "fibonacci-retracement",
            "elliott-wave",
            "gann-methods",
            "stock-to-flow",
        ]:
            assert registry.get(entry_id).verdict == "folklore"

    def test_golden_ratio_is_recorded_accurately_in_both_directions(self, registry):
        # It really does appear in TEA and RC5 as a provenance constant, and it
        # really does not carry a security property. Both halves matter.
        nutms = registry.get("golden-ratio-nutms")
        assert nutms.verdict == "provenance_only"
        assert any("TEA" in u for u in nutms.used_by)
        assert registry.get("fibonacci-retracement").verdict == "folklore"

    def test_lucas_sequences_are_the_one_real_fibonacci_dependency(self, registry):
        assert registry.get("lucas-sequences").verdict == "load_bearing"
        assert "lucas-sequences" in registry.get("primality-testing-bpsw").depends_on


class TestIntegrity:
    def test_ids_are_unique(self, raw):
        ids = [e["id"] for e in raw["entries"]]
        assert len(ids) == len(set(ids))

    def test_every_dependency_resolves(self, registry):
        for entry in registry:
            for dep in entry.depends_on:
                assert dep in registry, f"{entry.id} -> {dep}"

    def test_dependency_graph_is_acyclic(self, registry):
        # load_registry raises on a cycle, so reaching here proves acyclicity;
        # this walks every node to prove the traversal itself terminates.
        for entry in registry:
            registry.dependencies_of(entry.id)

    def test_an_implemented_entry_names_a_module_that_exists(self, registry):
        for entry in registry:
            if entry.status != "implemented":
                continue
            for owner in entry.owners:
                assert (PROJECT_ROOT / owner.module).exists(), f"{entry.id}: {owner.module}"

    def test_an_implemented_entry_has_exactly_one_owner(self, registry):
        for entry in registry:
            if entry.status == "implemented":
                assert len(entry.owners) == 1, entry.id

    def test_planned_and_implemented_entries_declare_wiring(self, registry):
        for entry in registry:
            if entry.status in {"implemented", "planned"}:
                assert entry.wiring, entry.id

    def test_security_relevant_entries_state_their_failure_mode(self, registry):
        for entry in registry:
            if entry.repo_relevance == "security":
                assert len(entry.risk_if_misused) >= 20, entry.id

    def test_folklore_entries_state_why_they_are_dangerous(self, registry):
        for entry in registry.gated():
            assert entry.risk_if_misused, entry.id


class TestFolkloreGate:
    """Numerology must not be able to become a live signal by accident."""

    def test_no_folklore_entry_is_marked_implemented(self, registry):
        for entry in registry.gated():
            assert entry.status != "implemented", entry.id

    def test_folklore_entries_are_flagged_as_gated(self, registry):
        assert registry.get("fibonacci-retracement").is_gated
        assert not registry.get("finite-fields").is_gated

    def test_loader_rejects_a_folklore_entry_claiming_implementation(self, tmp_path, raw):
        for entry in raw["entries"]:
            if entry["id"] == "fibonacci-retracement":
                entry["status"] = "implemented"
                entry["wiring"] = [{"module": "src/mathcore/registry.py", "kind": "owner"}]
        target = tmp_path / "registry.json"
        target.write_text(json.dumps(raw), encoding="utf-8")
        # Caught twice over: the schema forbids the status/verdict combination
        # and the semantic pass forbids it again. Either rejection is correct,
        # so the assertion is on the refusal, not on which layer refused.
        with pytest.raises(RegistryError):
            load_registry(target)


class TestLoaderRejectsDrift:
    """Each check exists because it is a way a registry rots in practice."""

    def _write(self, tmp_path: Path, raw: dict) -> Path:
        target = tmp_path / "registry.json"
        target.write_text(json.dumps(raw), encoding="utf-8")
        return target

    def test_dangling_dependency_is_rejected(self, tmp_path, raw):
        raw["entries"][0]["depends_on"] = ["no-such-entry"]
        with pytest.raises(RegistryError, match="unknown entries"):
            load_registry(self._write(tmp_path, raw))

    def test_cycle_is_rejected_and_named(self, tmp_path, raw):
        by_id = {e["id"]: e for e in raw["entries"]}
        by_id["finite-fields"]["depends_on"] = ["elliptic-curves"]
        with pytest.raises(RegistryError, match="dependency cycle"):
            load_registry(self._write(tmp_path, raw))

    def test_unknown_domain_is_rejected(self, tmp_path, raw):
        raw["entries"][0]["domain"] = "astrology"
        with pytest.raises(RegistryError):
            load_registry(self._write(tmp_path, raw))

    def test_duplicate_id_is_rejected(self, tmp_path, raw):
        raw["entries"].append(dict(raw["entries"][0]))
        with pytest.raises(RegistryError, match="duplicate"):
            load_registry(self._write(tmp_path, raw))

    def test_implemented_entry_with_a_missing_module_is_rejected(self, tmp_path, raw):
        raw["entries"][0]["status"] = "implemented"
        raw["entries"][0]["wiring"] = [
            {"module": "src/mathcore/does_not_exist.py", "kind": "owner"}
        ]
        with pytest.raises(RegistryError, match="does not exist"):
            load_registry(self._write(tmp_path, raw))


class TestSemanticChecksInIsolation:
    """
    The rejections the JSON Schema masks.

    load_registry validates structure before semantics, so several checks in
    _check_semantics can never fire through the public entry point -- the
    schema rejects the same document first. They are defence in depth: the
    schema and the semantic pass are separate layers, and a schema relaxed
    later must not silently take these with it. Exercising them directly is
    the only way to prove they still work.
    """

    def raw_with(self, entries: list[dict]) -> dict:
        return {
            "domains": ["algebra"],
            "verdict_definitions": {"load_bearing": "x" * 25, "folklore": "y" * 25},
            "entries": entries,
        }

    def entry(self, **overrides) -> dict:
        base = {
            "id": "thing",
            "name": "Thing",
            "domain": "algebra",
            "verdict": "load_bearing",
            "role": "r" * 40,
            "used_by": ["something"],
            "repo_relevance": "none",
            "status": "planned",
        }
        base.update(overrides)
        return base

    def check(self, entries: list[dict]) -> None:
        from src.mathcore.registry import RegistryEntry, _check_semantics

        raw = self.raw_with(entries)
        _check_semantics(raw, [RegistryEntry.from_dict(e) for e in entries])

    def test_unknown_verdict_is_rejected(self):
        entry = self.entry(verdict="vibes")
        with pytest.raises(RegistryError, match="no definition in verdict_definitions"):
            self.check([entry])

    def test_implemented_entry_with_no_owner_is_rejected(self):
        entry = self.entry(status="implemented", wiring=[])
        with pytest.raises(RegistryError, match="names no owning module"):
            self.check([entry])

    def test_implemented_entry_with_several_owners_is_rejected(self):
        entry = self.entry(
            status="implemented",
            wiring=[
                {"module": "src/mathcore/registry.py", "kind": "owner"},
                {"module": "scripts/generate_math_docs.py", "kind": "owner"},
            ],
        )
        with pytest.raises(RegistryError, match="claims several owners"):
            self.check([entry])

    def test_folklore_marked_implemented_is_rejected_by_the_semantic_pass(self):
        # Unreachable through load_registry: the schema forbids this pairing
        # first. Tested here so the second layer is known to work if the first
        # is ever relaxed.
        entry = self.entry(
            verdict="folklore",
            status="implemented",
            wiring=[{"module": "src/mathcore/registry.py", "kind": "owner"}],
        )
        with pytest.raises(RegistryError, match="folklore"):
            self.check([entry])


class TestQueryApi:
    def test_get_returns_the_entry(self, registry):
        assert registry.get("ntt").name.startswith("Number-theoretic")

    def test_get_suggests_near_matches(self, registry):
        with pytest.raises(RegistryError, match="Did you mean"):
            registry.get("ntt-transform")

    def test_unknown_id_raises(self, registry):
        with pytest.raises(RegistryError):
            registry.get("completely-unrelated")

    def test_transitive_dependencies_are_resolved(self, registry):
        deps = {e.id for e in registry.dependencies_of("hidden-number-problem")}
        assert "elliptic-curves" in deps
        assert "finite-fields" in deps, "transitive dependency was not followed"

    def test_dependents_supports_impact_analysis(self, registry):
        dependents = {e.id for e in registry.dependents_of("finite-fields")}
        assert "cyclic-groups-dlp" in dependents

    def test_by_relevance_filters_on_the_repo_relevance_field(self, registry):
        security = registry.by_relevance("security")
        assert security, "no security-relevant entries; the filter proves nothing"
        assert all(e.repo_relevance == "security" for e in security)
        assert not registry.by_relevance("not-a-relevance")

    def test_consumers_lists_only_consumer_wiring(self, registry):
        entry = registry.get("elliptic-curves")
        assert entry.consumers, "expected consumer wiring on this entry"
        assert all(w.kind == "consumer" for w in entry.consumers)
        assert all(w.kind == "owner" for w in entry.owners)

    def test_owned_by_finds_the_owning_entry(self, registry):
        owned = registry.owned_by("src/mathcore/fields/ntt.py")
        assert {e.id for e in owned} == {"ntt"}

    def test_entries_are_immutable(self, registry):
        # Frozen on purpose: a verdict that can be mutated at runtime is not a
        # governance control.
        with pytest.raises(dataclasses.FrozenInstanceError):
            registry.get("ntt").verdict = "folklore"  # type: ignore[misc]


class TestDefaultRegistry:
    def test_returns_a_loaded_registry(self):
        from src.mathcore.registry import default_registry

        default_registry.cache_clear()
        assert len(default_registry()) > 0

    def test_is_cached_so_validation_runs_once_per_process(self):
        from src.mathcore.registry import default_registry

        default_registry.cache_clear()
        assert default_registry() is default_registry()
        default_registry.cache_clear()


class TestDocumentationIsInSync:
    """
    The prose must describe the data.

    A registry whose companion document has drifted is worse than no document,
    because the document is what a reader actually reads.
    """

    def test_foundations_doc_exists(self):
        assert (PROJECT_ROOT / "docs" / "MATH_FOUNDATIONS.md").exists()

    def test_architecture_doc_exists(self):
        assert (PROJECT_ROOT / "docs" / "MATH_ARCHITECTURE.md").exists()

    def test_roadmap_doc_exists(self):
        assert (PROJECT_ROOT / "docs" / "MATH_ROADMAP.md").exists()

    def test_every_entry_id_appears_in_the_foundations_doc(self, registry):
        text = (PROJECT_ROOT / "docs" / "MATH_FOUNDATIONS.md").read_text(encoding="utf-8")
        missing = [e.id for e in registry if e.id not in text]
        assert missing == [], f"entries absent from the document: {missing}"

    def test_every_planned_owner_module_appears_in_the_architecture_doc(self, registry):
        text = (PROJECT_ROOT / "docs" / "MATH_ARCHITECTURE.md").read_text(encoding="utf-8")
        missing = sorted(
            {
                owner.module
                for entry in registry
                if entry.status in {"planned", "implemented"}
                for owner in entry.owners
                if owner.module.endswith(".py") and owner.module not in text
            }
        )
        assert missing == [], f"owner modules absent from the architecture: {missing}"
