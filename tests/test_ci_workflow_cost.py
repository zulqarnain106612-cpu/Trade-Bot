"""
The pull request's wall clock is a property worth a test.

A pull request used to wait ten to twelve minutes when everything was fine.
Almost none of that was testing: three of the five jobs installed the full
runtime dependency set -- a CPU torch wheel included -- in order to run `ruff
check`, `coverage report` and a handful of stdlib-only registry scripts. The
install is invisible in a green run, which is exactly why it survived.

These tests pin the shape that made it fast, so re-adding a dependency install
to a job that does not import the project fails here rather than showing up as
five minutes nobody attributes to anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"

# Jobs whose steps never import the project, and so must never install it.
STDLIB_ONLY_JOBS = ("python-lint", "python-coverage-floors", "architecture")


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _run_bodies(job: dict) -> str:
    return "\n".join(s.get("run", "") for s in job.get("steps", []))


class TestOnlyTheTestsInstallTheProject:
    @pytest.mark.parametrize("job_id", STDLIB_ONLY_JOBS)
    def test_a_stdlib_only_job_does_not_install_the_runtime_deps(self, spec, job_id):
        """
        generate_math_docs, generate_quality_docs, qe_gate,
        check_coverage_floors and arch_gate import nothing outside the
        standard library. Installing requirements.txt for them buys nothing
        and costs minutes on the critical path.
        """
        body = _run_bodies(spec["jobs"][job_id])
        assert "-r requirements.txt" not in body, job_id
        assert "torch" not in body, job_id

    def test_the_lint_job_takes_its_ruff_version_from_the_requirements_file(self, spec):
        """
        A ruff version repeated in the workflow drifts from the one developers
        run, and the drift shows up as a lint failure nobody can reproduce.
        """
        body = _run_bodies(spec["jobs"]["python-lint"])
        assert "requirements-dev.txt" in body
        assert "ruff==" in body

    def test_the_test_job_still_installs_everything(self, spec):
        """The saving is scope, not coverage: the suite gets the full set."""
        body = _run_bodies(spec["jobs"]["python-tests"])
        assert "-r requirements.txt" in body
        assert "-r requirements-dev.txt" in body


class TestTheExpensiveInstallIsCached:
    def test_the_test_job_installs_through_uv_with_its_cache_enabled(self, spec):
        """
        uv resolves the same pins pip does; what it adds is a wheel cache keyed
        on the requirements files, so an unchanged dependency set is unpacked
        rather than downloaded. Every shard pays this cost, so it is the one
        worth caching.
        """
        job = spec["jobs"]["python-tests"]
        uv = [s for s in job["steps"] if "astral-sh/setup-uv" in s.get("uses", "")]
        assert len(uv) == 1
        assert uv[0]["with"]["enable-cache"] is True
        assert "requirements.txt" in uv[0]["with"]["cache-dependency-glob"]
        assert _run_bodies(job).count("uv pip install --system") == 2

    def test_torch_is_taken_from_the_cpu_index_everywhere_it_is_installed(self):
        """
        PyPI's default torch wheel bundles the CUDA runtime -- gigabytes of
        download for a CPU runner that never executes a line of it. Every
        workflow that installs torch must name the CPU index first.
        """
        for path in (PROJECT_ROOT / ".github" / "workflows").glob("*.yml"):
            wf = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_id, job in (wf.get("jobs") or {}).items():
                body = _run_bodies(job)
                if "torch" not in body:
                    continue
                assert "download.pytorch.org/whl/cpu" in body, f"{path.name}:{job_id}"


class TestShardingActuallyShortensTheRun:
    def test_the_shard_list_and_the_declared_total_agree(self, spec):
        """
        pytest-split is told `--splits $SHARD_TOTAL`. A `total` that disagrees
        with the length of `shard` does not fail -- it silently runs part of
        the suite twice and another part never, which is a coverage hole
        wearing a green check.
        """
        matrix = spec["jobs"]["python-tests"]["strategy"]["matrix"]
        assert len(matrix["total"]) == 1
        assert matrix["total"][0] == len(matrix["shard"])
        assert matrix["shard"] == list(range(1, len(matrix["shard"]) + 1))

    def test_there_are_enough_shards_to_be_worth_sharding(self, spec):
        matrix = spec["jobs"]["python-tests"]["strategy"]["matrix"]
        assert len(matrix["shard"]) >= 6


class TestSupersededWorkIsCancelled:
    def test_a_new_push_cancels_the_previous_run_on_a_branch(self, spec):
        """
        Two runs of the same branch compete for the same runners, and the
        older one is answering a question nobody is asking any more. main is
        exempt: its history has to be checked commit by commit.
        """
        concurrency = spec["concurrency"]
        assert "cancel-in-progress" in concurrency
        assert "refs/heads/main" in str(concurrency["cancel-in-progress"])
