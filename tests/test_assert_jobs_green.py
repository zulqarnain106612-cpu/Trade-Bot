"""
Tests for the all-green gate.

Two halves, and both matter. The first is the script's own logic: a gate that
passes when it should not is worse than no gate, because it is trusted. The
second is the wiring: a gate that silently stops covering a job -- because
someone added a job and did not add it to `needs:` -- fails open, and nothing
about the green check would show it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "assert_jobs_green.py"
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"

#: Workflows that never run on a pull request, so a gate there guards nothing.
NO_GATE_EXPECTED = {"rag-ingest.yml"}


def run_gate(needs: dict | str, allow_skipped: str | None = None) -> subprocess.CompletedProcess:
    payload = needs if isinstance(needs, str) else json.dumps(needs)
    env = {"NEEDS": payload, "PATH": "/usr/bin:/bin:/usr/local/bin"}
    if allow_skipped is not None:
        env["ALLOW_SKIPPED"] = allow_skipped
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


class TestPasses:
    def test_all_success_exits_zero(self):
        result = run_gate({"a": {"result": "success"}, "b": {"result": "success"}})
        assert result.returncode == 0
        assert "All jobs green" in result.stdout

    def test_single_success_exits_zero(self):
        assert run_gate({"only": {"result": "success"}}).returncode == 0

    def test_allowed_skip_passes(self):
        result = run_gate(
            {"ctx": {"result": "skipped"}, "review": {"result": "success"}},
            allow_skipped="ctx",
        )
        assert result.returncode == 0
        assert "skip allowed" in result.stdout

    def test_allow_skipped_tolerates_spacing(self):
        result = run_gate(
            {"a": {"result": "skipped"}, "b": {"result": "skipped"}},
            allow_skipped=" a , b ",
        )
        assert result.returncode == 0


class TestFails:
    @pytest.mark.parametrize("bad", ["failure", "cancelled", "skipped"])
    def test_any_non_success_fails(self, bad):
        result = run_gate({"good": {"result": "success"}, "bad": {"result": bad}})
        assert result.returncode == 1
        assert "bad" in result.stderr

    def test_a_skip_not_on_the_allow_list_fails(self):
        result = run_gate(
            {"ctx": {"result": "skipped"}, "other": {"result": "skipped"}},
            allow_skipped="ctx",
        )
        assert result.returncode == 1
        assert "other" in result.stderr
        assert "ctx" not in result.stderr

    def test_allowed_job_that_fails_is_not_excused(self):
        # The allowance is for skipping, not for failing.
        result = run_gate({"ctx": {"result": "failure"}}, allow_skipped="ctx")
        assert result.returncode == 1

    def test_missing_result_key_fails(self):
        result = run_gate({"a": {}})
        assert result.returncode == 1

    def test_failures_are_reported_before_skips(self):
        result = run_gate(
            {"s": {"result": "skipped"}, "f": {"result": "failure"}},
        )
        lines = [ln for ln in result.stderr.splitlines() if ln.startswith("  ")]
        assert lines[0].strip().startswith("f:"), lines

    def test_every_offender_is_named(self):
        result = run_gate(
            {
                "one": {"result": "failure"},
                "two": {"result": "cancelled"},
                "three": {"result": "skipped"},
                "four": {"result": "success"},
            }
        )
        assert result.returncode == 1
        for job in ("one", "two", "three"):
            assert job in result.stderr


class TestMalformedInputIsNeverAPass:
    """A gate that cannot evaluate has verified nothing; it must not exit 0."""

    def test_missing_needs_exits_two(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin"},
            timeout=30,
            check=False,
        )
        assert result.returncode == 2

    def test_invalid_json_exits_two(self):
        assert run_gate("{not json").returncode == 2

    def test_non_object_json_exits_two(self):
        assert run_gate("[1, 2, 3]").returncode == 2

    def test_empty_object_exits_two(self):
        # An empty needs map would otherwise pass vacuously and give a false
        # all-clear for a gate wired to nothing.
        result = run_gate("{}")
        assert result.returncode == 2
        assert "depends on no jobs" in result.stderr


class TestStaleAllowance:
    def test_unused_allowance_is_reported(self):
        result = run_gate({"a": {"result": "success"}}, allow_skipped="gone")
        assert result.returncode == 0
        assert "gone" in result.stdout
        assert "stale allowance" in result.stdout


def workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def runs_on_pull_request(spec: dict) -> bool:
    # PyYAML parses the bare key `on:` as the boolean True.
    triggers = spec.get("on", spec.get(True, {}))
    return "pull_request" in (triggers or {})


class TestEveryPullRequestWorkflowIsGated:
    def test_at_least_one_workflow_is_checked(self):
        assert workflow_files(), "no workflows found; the wiring tests would be vacuous"

    @pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
    def test_pull_request_workflows_have_a_gate(self, path):
        spec = load(path)
        if path.name in NO_GATE_EXPECTED or not runs_on_pull_request(spec):
            pytest.skip(f"{path.name} does not run on pull requests")
        assert "gate" in spec["jobs"], f"{path.name} has no gate job"

    @pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
    def test_gate_covers_every_job_in_its_workflow(self, path):
        # The failure this catches: someone adds a job and forgets `needs:`,
        # so the gate goes green while not watching the new job at all.
        spec = load(path)
        jobs = spec.get("jobs", {})
        if "gate" not in jobs:
            pytest.skip(f"{path.name} has no gate job")
        covered = set(jobs["gate"]["needs"])
        expected = set(jobs) - {"gate"}
        assert covered == expected, f"{path.name}: gate misses {expected - covered}"

    @pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
    def test_gate_runs_even_when_a_dependency_fails(self, path):
        # Without `if: always()` the gate is skipped as soon as anything it
        # needs fails, and a skipped required check blocks nothing.
        spec = load(path)
        jobs = spec.get("jobs", {})
        if "gate" not in jobs:
            pytest.skip(f"{path.name} has no gate job")
        assert jobs["gate"].get("if") == "always()", path.name

    @pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
    def test_gate_invokes_the_shared_script(self, path):
        spec = load(path)
        jobs = spec.get("jobs", {})
        if "gate" not in jobs:
            pytest.skip(f"{path.name} has no gate job")
        runs = " ".join(step.get("run", "") for step in jobs["gate"]["steps"])
        assert "scripts/assert_jobs_green.py" in runs, path.name

    @pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
    def test_gate_passes_the_needs_context(self, path):
        spec = load(path)
        jobs = spec.get("jobs", {})
        if "gate" not in jobs:
            pytest.skip(f"{path.name} has no gate job")
        envs = [step.get("env", {}) for step in jobs["gate"]["steps"]]
        assert any("toJSON(needs)" in str(env.get("NEEDS", "")) for env in envs), path.name

    @pytest.mark.parametrize("path", workflow_files(), ids=lambda p: p.name)
    def test_allow_skipped_only_names_real_jobs(self, path):
        # A stale exemption is a hole nobody is watching.
        spec = load(path)
        jobs = spec.get("jobs", {})
        if "gate" not in jobs:
            pytest.skip(f"{path.name} has no gate job")
        for step in jobs["gate"]["steps"]:
            raw = step.get("env", {}).get("ALLOW_SKIPPED")
            if not raw:
                continue
            named = {part.strip() for part in raw.split(",") if part.strip()}
            assert named <= set(jobs) - {"gate"}, f"{path.name}: unknown {named}"
