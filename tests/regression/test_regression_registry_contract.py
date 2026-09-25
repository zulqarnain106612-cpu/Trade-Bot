"""
GOV-001, GOV-002, GOV-006, GOV-007 — the continuous-improvement loop.

```
Production → Metrics → Defects → Incidents → Root cause
    → Corrective action → New test/control → CI gate → Release → Production
```

Every failure should make the system harder to break again. That only happens
if the loop has teeth, so this module tests the mechanism rather than any one
defect:

- a `REG-####` or `SEC-####` entry cannot claim a test that does not exist
  (the registry loader), and cannot exist without one;
- a closed entry names the verification layer that should have caught it, so
  the metric that matters — how many defects escaped each layer — is
  computable;
- the metrics collector reports what it cannot measure as *unavailable*
  rather than omitting it, because a report that quietly drops a metric reads
  as a clean bill of health;
- the root-cause template exists and stops at a control rather than a person.

There are no REG/SEC entries yet. That is the honest state of a registry
created three phases ago, and the tests below assert the *contract* those
entries will be held to, so the first one filed lands on rails rather than
inventing its own shape.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from src.quality.registry import PREFIX_KINDS, RegistryError, load_registry

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = PROJECT_ROOT / "docs" / "quality" / "ROOT_CAUSE_TEMPLATE.md"
COLLECTOR = PROJECT_ROOT / "scripts" / "collect_quality_metrics.py"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def registry():
    return load_registry()


@pytest.fixture(scope="module")
def collector() -> ModuleType:
    return _load(COLLECTOR, "collect_quality_metrics")


class TestTheRegistryHasAPlaceForDefects:
    def test_both_defect_kinds_are_declared(self, registry):
        assert "regression" in registry.kind_definitions
        assert "security_regression" in registry.kind_definitions

    def test_the_id_prefixes_are_bound_to_those_kinds(self):
        # A REG-#### that declares itself a requirement would be filed among
        # the requirements and never counted as a defect.
        assert PREFIX_KINDS["REG"] == "regression"
        assert PREFIX_KINDS["SEC"] == "security_regression"

    def test_the_definitions_say_the_test_is_permanent(self, registry):
        # The rule that distinguishes a regression test from a bug report.
        assert "never deleted" in registry.kind_definitions["regression"]
        assert "permanent" in registry.kind_definitions["security_regression"]

    def test_the_filed_defects_are_the_ones_we_know_about(self, registry):
        # This test is the visible event. It used to assert that no defect had
        # been filed; updating it is how filing one announces itself, rather
        # than a defect appearing by silent append. Each id is listed here on
        # purpose, so a new entry cannot ride in unnoticed on a passing suite.
        #
        # REG-0005: an order-dependent test read a process-global registry an
        # earlier test had written, so it passed while checking the wrong
        # blend weight. It was filed as REG-0001 on this branch before the
        # queue defects took that id on main; the entry was renumbered in the
        # merge rather than dropped.
        #
        # REG-0007: the lint job was trimmed to ruff alone and stopped
        # installing jsonschema and PyYAML, which qe_gate hard-depends on. It
        # passed locally because the developer's interpreter already had both,
        # so only a clean environment could see it. REG-0006 is deliberately
        # skipped: it is taken by the queue-promotion defect on another branch,
        # and reusing the id would collide when that branch lands.
        # REG-0008: _approximate_sopr returned a literal 1.0 and ignored its
        # argument, and that value reached the live feature dict in src/intel.py
        # and the intelligence_features_history table. The test that existed
        # asserted the placeholder, so the suite could not have caught it.
        # REG-0009: xdist workers inherited the controller's DUCKDB_PATH, so
        # they all opened one exclusively-locked DuckDB file and whichever
        # worker arrived second failed. Only visible under -n, so it read as a
        # flake rather than as the isolation defect it was.
        # REG-0010: qe_new_requirement.py allocated the next id by scanning the
        # work tree, which sees only what has merged. Two branches open at once
        # were handed the same id three times in one session; a human reading
        # the diff caught every one, and nothing else would have.
        # REG-0011: the same scaffolder reported success against the JSON
        # schema while the gate validates with the loader, so it printed
        # "[ok  ] added SEC-0001" for an entry the loader then refused.
        # REG-0012: GOV-015 pinned the CPU torch index for every workflow that
        # installs torch, and its test globs .github/workflows. The Dockerfile
        # installs "-r requirements.txt" and never names torch, so the largest
        # torch install in the repository sat outside both the rule's wording
        # and its test until the container job died on "No space left on
        # device".
        # REG-0013: scripts/timescaledb.sh was named from six call sites --
        # the README, two src/ comments and the skip message all 92
        # TimescaleDB tests printed -- and was not in the repository, so the
        # backend reported green by absence and the instruction the reader
        # was given could not be followed.
        # REG-0014: initialize() loaded both exchange venues in one unisolated
        # sequence, so Binance answering 451 from a restricted location aborted
        # startup entirely and stopped OKX strategies that never touched
        # Binance -- and recovery meant restarting the process.
        # REG-0015: the same shape as REG-0014 one layer further in. The
        # ensemble fit was submitted to the shared single-worker training
        # executor with no timeout, guarded only by `except Exception` --
        # which cannot catch a hang. A wedged fit held the FastAPI lifespan
        # open forever, so port 8000 never opened and the health endpoint,
        # living behind that same lifespan, could not be asked. Nothing in
        # the suite bounded a training call, so only a hang in the wild
        # would have shown it.
        assert {e.id for e in registry.by_kind("regression")} == {
            "REG-0005",
            "REG-0007",
            "REG-0008",
            "REG-0009",
            "REG-0010",
            "REG-0011",
            "REG-0012",
            "REG-0013",
            "REG-0014",
            "REG-0015",
        }
        assert {e.id for e in registry.by_kind("security_regression")} == set()

    def test_every_filed_defect_names_a_permanent_test(self, registry):
        # The rule that separates a regression entry from a bug report: the
        # test outlives the fix. `verified` already forces the file to exist;
        # this asserts the entry is not sitting at `planned` with nothing
        # deciding it.
        for entry in registry.by_kind("regression") + registry.by_kind("security_regression"):
            assert entry.status in {"verified", "partial"}, entry.id
            assert entry.verification, entry.id


class TestADefectEntryCannotClaimATestItDoesNotHave:
    """The loader's contract, exercised on the shapes a defect entry takes."""

    @staticmethod
    def _write(tmp_path: Path, entry: dict) -> Path:
        base = {
            "registry_version": "1.0.0",
            "kind_definitions": {
                "requirement": "A property the system must have, stated testably.",
                "invariant": "A trading invariant that holds on every path.",
                "regression": "A defect that reached a running system once.",
                "security_regression": "A security weakness that was once possible.",
            },
            "status_definitions": {
                "verified": "A named test exists on disk and covers the statement.",
                "partial": "Some verification exists; planned_in finishes the job.",
                "planned": "No test yet; planned_in names the PR that adds one.",
                "accepted_gap": "A deliberate, dated decision not to verify.",
            },
            "criticality_definitions": {
                "critical": "Violation can lose money or leak a credential.",
                "high": "Violation degrades a safety control.",
                "medium": "Violation costs correctness with bounded impact.",
                "low": "Violation is a hygiene concern.",
            },
            "test_types": {"regression": "Did an old bug come back?"},
            "subsystems": ["execution"],
            "phases": {"PR-006": "Regression + Property Testing"},
            "entries": [entry],
        }
        target = tmp_path / "registry.json"
        target.write_text(json.dumps(base), encoding="utf-8")
        return target

    def test_a_regression_naming_a_missing_test_is_refused(self, tmp_path):
        (tmp_path / "tests" / "regression").mkdir(parents=True)
        path = self._write(
            tmp_path,
            {
                "id": "REG-0001",
                "kind": "regression",
                "title": "Duplicate orders on retry",
                "statement": "A retried execution request must not create a second position.",
                "criticality": "critical",
                "subsystem": "execution",
                "status": "verified",
                "source": "QE-48",
                # Required by the registry schema since it gained the
                # cross-field rules: an entry nobody can describe the
                # failure of is one nobody can size the priority of.
                "failure_mode": "A retry creates a second position and the book is wrong.",
                "verification": [
                    {
                        "test": "tests/regression/REG_0001_duplicate_orders.py",
                        "test_type": "regression",
                    }
                ],
            },
        )
        with pytest.raises(RegistryError, match="not on disk"):
            load_registry(path=path, root=tmp_path)

    def test_a_regression_with_its_test_on_disk_loads(self, tmp_path):
        target = tmp_path / "tests" / "regression" / "REG_0001_duplicate_orders.py"
        target.parent.mkdir(parents=True)
        target.write_text("", encoding="utf-8")
        path = self._write(
            tmp_path,
            {
                "id": "REG-0001",
                "kind": "regression",
                "title": "Duplicate orders on retry",
                "statement": "A retried execution request must not create a second position.",
                "criticality": "critical",
                "subsystem": "execution",
                "status": "verified",
                "source": "QE-48",
                # Required by the registry schema since it gained the
                # cross-field rules: an entry nobody can describe the
                # failure of is one nobody can size the priority of.
                "failure_mode": "A retry creates a second position and the book is wrong.",
                "notes": "layer: property",
                "verification": [
                    {
                        "test": "tests/regression/REG_0001_duplicate_orders.py",
                        "test_type": "regression",
                    }
                ],
            },
        )
        loaded = load_registry(path=path, root=tmp_path)
        assert loaded.get("REG-0001").is_verified

    def test_a_regression_with_no_test_and_no_owner_is_refused(self, tmp_path):
        # "We know about it" is not a closed incident.
        (tmp_path / "tests").mkdir()
        path = self._write(
            tmp_path,
            {
                "id": "REG-0002",
                "kind": "regression",
                "title": "Something went wrong once",
                "statement": "A defect occurred and has not been pinned by any test yet.",
                "criticality": "high",
                "subsystem": "execution",
                "status": "planned",
                "source": "QE-48",
                # Required by the registry schema since it gained the
                # cross-field rules: an entry nobody can describe the
                # failure of is one nobody can size the priority of.
                "failure_mode": "A retry creates a second position and the book is wrong.",
            },
        )
        with pytest.raises(RegistryError, match="planned_in"):
            load_registry(path=path, root=tmp_path)

    def test_a_critical_defect_cannot_be_waived(self, tmp_path):
        (tmp_path / "tests").mkdir()
        path = self._write(
            tmp_path,
            {
                "id": "SEC-0001",
                "kind": "security_regression",
                "title": "Unauthenticated access to a sensitive endpoint",
                "statement": "An unauthenticated caller must not reach the trade endpoint.",
                "criticality": "critical",
                "subsystem": "execution",
                "status": "accepted_gap",
                "source": "QE-73",
                "waiver": {
                    "reason": "We would rather not write this test right now, honestly.",
                    "accepted_by": "operator",
                    "review_by": "2027-01-01",
                },
            },
        )
        with pytest.raises(RegistryError, match="cannot be waived"):
            load_registry(path=path, root=tmp_path)


@pytest.fixture(scope="module", name="template_text")
def _template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


class TestTheRootCauseTemplate:
    @pytest.fixture
    def text(self, template_text) -> str:
        return template_text

    def test_it_exists(self):
        assert TEMPLATE.exists()

    def test_it_asks_all_five_questions(self, text):
        for question in (
            "What happened?",
            "Why wasn't it detected?",
            "Why did the existing control fail?",
            "Why did the design permit it?",
            "What new control prevents recurrence?",
        ):
            assert question in text

    def test_it_refuses_to_stop_at_a_person(self, text):
        assert "stop at a control, not at a person" in text
        assert "Why could one mistake reach production?" in text

    def test_it_requires_the_layer_that_should_have_caught_it(self, text):
        # The input to the metric the document calls the most important one.
        assert "layer: <name>" in text or "layer: <the layer" in text

    def test_it_requires_the_test_to_fail_before_the_fix(self, text):
        # A regression test written after the fix, against the fixed code,
        # proves only that the code currently passes it.
        assert "fails against the pre-fix" in text

    def test_it_states_that_regression_tests_are_permanent(self, text):
        assert "never deleted because the bug is fixed" in text


class TestTheMetricsCollector:
    def test_it_runs(self, collector):
        report = collector.collect()
        assert report["metrics"]
        assert report["collected_at"]

    def test_it_reports_the_registry_counts(self, collector, registry):
        metrics = collector.collect()["metrics"]
        assert metrics["requirements_total"]["value"] == len(registry)
        assert metrics["requirements_waived"]["value"] == 0

    def test_an_unmeasurable_metric_is_reported_not_omitted(self, collector):
        # A report that quietly drops what it could not measure reads as a
        # clean bill of health.
        metrics = collector.collect()["metrics"]
        for name in ("mean_time_to_recover_s", "rollback_rate", "mutation_score"):
            assert name in metrics
            assert metrics[name]["status"] == "unavailable"
            assert metrics[name]["reason"]

    def test_a_filed_defect_is_counted_against_its_layer(self, collector):
        # Every filed defect names the layer that should have caught it. An
        # "unknown" bucket would mean an entry skipped that question, which
        # makes the metric useless: it measures which layer needs work.
        metric = collector.collect()["metrics"]["escaped_defects_by_layer"]
        assert metric["status"] == "ok"
        assert "unknown" not in metric["value"]
        # 8 -> 9 with REG-0015: an unbounded ensemble fit inside the startup
        # lifespan. The suite bounded no training call, so nothing but a hang
        # in the wild could have surfaced it -- test-suite, not review.
        assert metric["value"] == {"test-suite": 9, "review": 1}

    def test_zero_escaped_defects_would_be_stated_explicitly(self, collector, monkeypatch):
        # "We have not measured this" and "this is zero" are different
        # statements, and only one of them is good news. Once a defect is
        # filed the live registry can no longer reach that branch, so drive it
        # with a registry that has none rather than dropping the guarantee.
        monkeypatch.setattr(
            collector, "load_registry", lambda: SimpleNamespace(by_kind=lambda _kind: ())
        )
        metric = collector.escaped_defects()["escaped_defects_by_layer"]
        assert metric["status"] == "ok"
        assert metric["value"] == {}
        assert "explicit zero" in metric["note"]

    def test_it_counts_critical_requirements_with_no_test(self, collector):
        metric = collector.collect()["metrics"]["critical_unverified"]
        assert metric["status"] == "ok"
        assert isinstance(metric["value"], int)

    def test_it_writes_a_json_report(self, collector, tmp_path):
        target = tmp_path / "metrics.json"
        target.write_text(json.dumps(collector.collect(), indent=2), encoding="utf-8")
        assert json.loads(target.read_text(encoding="utf-8"))["metrics"]

    def test_the_script_runs_as_a_command(self):
        result = subprocess.run(
            [sys.executable, str(COLLECTOR)],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=600,
            check=False,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        assert "requirements_total" in result.stdout


@pytest.fixture(scope="module", name="mutation_checker")
def _mutation_checker() -> ModuleType:
    return _load(PROJECT_ROOT / "scripts" / "check_mutation_score.py", "check_mutation_score")


class TestTheMutationFloors:
    @pytest.fixture
    def checker(self, mutation_checker) -> ModuleType:
        return mutation_checker

    def test_every_declared_target_exists_on_disk(self, checker):
        # A path that no longer exists generates no mutants, and a subsystem
        # with no mutants would score a silent 0/0.
        config = checker.load_thresholds()
        missing = [
            path
            for spec in config["subsystems"].values()
            for path in spec["paths"]
            if not (PROJECT_ROOT / path).exists()
        ]
        assert not missing, missing

    def test_the_floors_match_the_source_document(self, checker):
        floors = checker.load_thresholds()["subsystems"]
        assert floors["risk"]["min_score"] == 0.90
        assert floors["execution"]["min_score"] == 0.90
        assert floors["signal"]["min_score"] == 0.85

    def test_every_subsystem_names_the_requirement_it_serves(self, checker):
        for spec in checker.load_thresholds()["subsystems"].values():
            assert spec["requirement"]

    def test_a_run_with_no_mutants_is_not_a_pass(self, checker):
        # The specific way this check could go green while proving nothing.
        result = checker.MutationResult(
            subsystem="empty", killed=0, survived=0, timeout=0, min_score=0.9, requirement="X"
        )
        assert not result.passed
        assert result.score == 0.0

    def test_a_timeout_counts_against_the_score(self, checker):
        # A timeout is a mutant the suite did not kill within the budget.
        # Counting it as killed would let a slow suite buy a score.
        result = checker.MutationResult(
            subsystem="s", killed=9, survived=0, timeout=1, min_score=0.95, requirement="X"
        )
        assert result.total == 10
        assert result.score == pytest.approx(0.9)
        assert not result.passed

    def test_a_clearing_subsystem_passes(self, checker):
        result = checker.MutationResult(
            subsystem="s", killed=95, survived=5, timeout=0, min_score=0.90, requirement="X"
        )
        assert result.passed
        assert "ok" in result.line()

    def test_the_counts_are_parsed_from_a_results_run(self, checker):
        counts = checker.parse_counts("killed: 120  survived: 8  timeout: 1")
        assert counts == {"killed": 120, "survived": 8, "timeout": 1}

    def test_an_unparseable_summary_yields_zeroes_not_a_pass(self, checker):
        assert checker.parse_counts("something else entirely") == {
            "killed": 0,
            "survived": 0,
            "timeout": 0,
        }
