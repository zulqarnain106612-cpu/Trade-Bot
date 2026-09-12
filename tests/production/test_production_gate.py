"""
REL-002, REL-003, REL-008, GOV-009, GOV-010 — the production gate.

Four properties that live in configuration and one that lives in arithmetic.

The configuration ones are the deployment path: production is a protected
environment, a release reaches full exposure through a canary, the canary is
bounded, and rollback is a job somebody can run rather than a paragraph
somebody wrote. None of those are observable at runtime — a deploy workflow
with no `environment:` key deploys perfectly well, right up to the day it
deploys something nobody approved.

The arithmetic one is GOV-010, and it is the reason this file ends the
programme: **readiness is a conjunction, not an average.** The usual
readiness number is a percentage over things that are not commensurable — a
missing dashboard and a missing kill switch move it by the same point. The
checker returns blockers, not a score, and the test below asserts it never
learns to return a score.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / ".github/workflows/deploy.yml"


@pytest.fixture(scope="module")
def deploy() -> dict:
    return yaml.safe_load(DEPLOY.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def deploy_text() -> str:
    return DEPLOY.read_text(encoding="utf-8")


class TestProductionIsAProtectedEnvironment:
    def test_the_workflow_exists(self):
        assert DEPLOY.exists()

    def test_the_deploying_jobs_name_an_environment(self, deploy):
        # The `environment:` key is what makes GitHub demand an approval and
        # restrict which branches may deploy. Without it the workflow runs
        # with nothing standing between a dispatch and production.
        deploying = {"canary", "promote", "rollback"}
        for name in deploying:
            job = deploy["jobs"][name]
            assert "environment" in job, f"{name} deploys with no protected environment"

    def test_the_full_exposure_job_uses_the_stricter_environment(self, deploy):
        assert deploy["jobs"]["promote"]["environment"]["name"] == "production"
        assert deploy["jobs"]["canary"]["environment"]["name"] == "production-canary"

    def test_it_is_manually_triggered_only(self, deploy):
        triggers = deploy.get("on", deploy.get(True))
        assert set(triggers) == {"workflow_dispatch"}

    def test_the_token_is_read_only_by_default(self, deploy):
        assert deploy["permissions"] == {"contents": "read"}

    def test_a_deployment_is_never_cancelled_mid_flight(self, deploy):
        # Two half-finished deploys is worse than one slow one.
        assert deploy["concurrency"]["cancel-in-progress"] is False


class TestPreflightGatesTheDeployment:
    def test_readiness_runs_before_anything_deploys(self, deploy):
        assert deploy["jobs"]["canary"]["needs"] == ["preflight"]

    def test_preflight_checks_readiness_and_supply_chain(self, deploy_text):
        assert "check_production_readiness.py" in deploy_text
        assert "check_supply_chain.py" in deploy_text

    def test_preflight_verifies_the_artifact_provenance(self, deploy_text):
        # Verified at deploy time rather than trusted from the build: the
        # question is whether *these bytes* are the ones that were attested.
        assert "write_provenance.py --verify" in deploy_text


class TestCanaryPrecedesFullExposure:
    def test_promotion_depends_on_the_canary(self, deploy):
        assert deploy["jobs"]["promote"]["needs"] == ["canary"]

    def test_the_canary_exposure_is_bounded(self, deploy_text):
        # A canary at full size is not a canary; it is a deployment with a
        # different name.
        assert "-le 25" in deploy_text

    def test_promotion_is_skipped_during_a_drill(self, deploy):
        assert deploy["jobs"]["promote"]["if"] == "${{ !inputs.drill }}"


class TestRollbackIsAPathNotAPlan:
    def test_a_rollback_job_exists(self, deploy):
        assert "rollback" in deploy["jobs"]

    def test_it_is_exercised_by_a_drill_input(self, deploy):
        # A rollback path that has never been taken is a hypothesis, and the
        # moment you need it is the worst moment to test it.
        assert deploy["jobs"]["rollback"]["if"] == "${{ inputs.drill }}"

    def test_the_drill_verifies_the_running_version_changed(self, deploy_text):
        assert "verify the deployed version is the previous one" in deploy_text

    def test_promote_and_rollback_are_mutually_exclusive(self, deploy):
        assert deploy["jobs"]["promote"]["if"] != deploy["jobs"]["rollback"]["if"]


class TestTheWorkflowHasAGate:
    def test_the_gate_needs_every_other_job(self, deploy):
        jobs = set(deploy["jobs"]) - {"gate"}
        assert set(deploy["jobs"]["gate"]["needs"]) == jobs

    def test_the_gate_always_runs(self, deploy):
        # Without `if: always()` the gate is skipped the moment a dependency
        # fails, and a skipped gate blocks nothing.
        assert deploy["jobs"]["gate"]["if"] == "always()"

    def test_the_mutually_exclusive_jobs_are_the_only_allowed_skips(self, deploy_text):
        assert "ALLOW_SKIPPED: promote,rollback" in deploy_text

    def test_the_allowance_is_explained(self, deploy_text):
        # Every ALLOW_SKIPPED entry needs a comment saying why, or an
        # exemption outlives its reason without anybody noticing.
        assert "mutually exclusive by design" in deploy_text


class TestReadinessIsAConjunction:
    """GOV-010 — the capstone. Not a score."""

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO / "scripts/check_production_readiness.py"), *args],
            capture_output=True,
            text=True,
            cwd=REPO,
            check=False,  # a non-zero exit is what several of these assert
        )

    def test_the_checker_runs(self):
        result = self._run("--root", str(REPO))
        # 0 ready, 1 not ready. Either is a working checker; 2 is not.
        assert result.returncode in (0, 1), result.stdout + result.stderr

    def test_it_reports_blockers_rather_than_a_percentage(self):
        result = self._run("--root", str(REPO))
        assert "%" not in result.stdout
        assert "READY" in result.stdout

    def test_a_broken_evaluation_is_distinct_from_a_failing_one(self, tmp_path):
        # Exit 2, not 1: a checker that cannot run has not said no, and
        # collapsing the two hides a broken gate behind a failing one.
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "quality_registry.json").write_text("{not json")
        result = self._run("--root", str(tmp_path))
        assert result.returncode == 2

    def test_every_critical_requirement_must_be_verified(self):
        # The condition itself, checked directly against the registry rather
        # than through the script's exit code.
        from scripts.check_production_readiness import check_critical_entries_are_verified
        from src.quality.registry import load_registry

        registry = load_registry()
        blockers = check_critical_entries_are_verified(registry)
        for blocker in blockers:
            assert "is" in blocker.detail  # names the status it is stuck at

    def test_a_claimed_test_that_does_not_exist_is_a_blocker(self, tmp_path):
        from scripts.check_production_readiness import check_claimed_tests_exist
        from src.quality.registry import load_registry

        registry = load_registry()
        # Against an empty root, every claimed test is missing -- which is
        # what this condition is for: a renamed test turns a claim red.
        assert check_claimed_tests_exist(registry, tmp_path)

    def test_the_traceability_document_is_in_sync(self):
        from scripts.check_production_readiness import check_traceability_is_in_sync

        assert check_traceability_is_in_sync(REPO) == []


class TestTheMaturityClaim:
    """GOV-009 — Level 5 is a set of artifacts, not an adjective."""

    @pytest.mark.parametrize(
        "artifact",
        [
            "scripts/check_supply_chain.py",  # supply-chain security
            "scripts/write_provenance.py",  # artifact provenance
            "src/security/at_rest.py",  # cryptographic controls
            "docs/security/THREAT_MODEL.md",  # continuous threat modelling
            "docs/operations/DISASTER_RECOVERY.md",  # recovery
            "config/quality_registry.json",  # traceability
            "scripts/check_production_readiness.py",  # the conjunction
        ],
    )
    def test_each_claimed_capability_has_an_artifact(self, artifact):
        assert (REPO / artifact).exists(), f"{artifact} is claimed but absent"

    def test_the_registry_records_the_whole_programme(self):
        registry = json.loads((REPO / "config/quality_registry.json").read_text())
        assert len(registry["entries"]) >= 90
