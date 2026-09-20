"""
The failure notice is the thing that makes not-polling safe.

GOV-011 and GOV-012 forbid an agent from going and looking at CI: no live
watch, and no unbounded log fetch. That is only workable if failures come to
the pull request on their own. These tests pin the properties that make the
notice trustworthy -- it fires on every not-green conclusion, never on a green
one, stays inside the same line budget the command policy enforces, and
replaces its own prior comment instead of stacking new ones.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci-failure-notify.yml"


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def script(spec: dict) -> str:
    steps = spec["jobs"]["notify"]["steps"]
    bodies = [s.get("with", {}).get("script", "") for s in steps]
    return "\n".join(b for b in bodies if b)


class TestTrigger:
    def test_it_runs_on_workflow_run_completion(self, spec):
        # PyYAML parses a bare `on:` key as the boolean True.
        triggers = spec.get("on") or spec.get(True)
        assert "workflow_run" in triggers
        assert triggers["workflow_run"]["types"] == ["completed"]

    def test_it_watches_every_workflow_that_gates_a_pull_request(self, spec):
        triggers = spec.get("on") or spec.get(True)
        watched = set(triggers["workflow_run"]["workflows"])

        gating = set()
        for path in (PROJECT_ROOT / ".github" / "workflows").glob("*.yml"):
            if path == WORKFLOW:
                continue
            wf = yaml.safe_load(path.read_text(encoding="utf-8"))
            on = wf.get("on") or wf.get(True) or {}
            if "pull_request" in on:
                gating.add(wf["name"])

        missing = gating - watched
        assert not missing, f"pull-request workflows with no failure notice: {sorted(missing)}"

    @pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
    def test_every_not_green_conclusion_is_reported(self, spec, conclusion):
        """CLAUDE.md treats not-green as the failure condition, not just red."""
        assert f"'{conclusion}'" in spec["jobs"]["notify"]["if"]

    def test_a_green_run_is_silent(self, spec):
        """
        A notification that fires on success is one people learn to ignore.

        Asserted by absence of any success branch in the condition: the `if`
        is an allowlist of not-green conclusions, so success cannot match it.
        """
        condition = spec["jobs"]["notify"]["if"]
        assert "success" not in condition


class TestPermissions:
    def test_it_can_comment_but_not_write_code(self, spec):
        perms = spec["permissions"]
        assert perms["pull-requests"] == "write"
        assert perms["contents"] == "read"
        assert perms["actions"] == "read"


class TestCommentBody:
    def test_the_comment_is_capped(self, script):
        """
        The notice is read into an agent's context, so it obeys the same
        budget as every other read: bounded, and bounded by a constant that
        cannot silently grow past the policy limit.
        """
        assert "const MAX = 20" in script
        assert "slice(0, MAX)" in script

        policy = json.loads(
            (PROJECT_ROOT / "config" / "command_policy.json").read_text(encoding="utf-8")
        )
        assert policy["bounded_output"]["max_declared_lines"] >= 20

    def test_it_updates_its_prior_notice_rather_than_stacking(self, script):
        assert "updateComment" in script
        assert "ci-failure-notice:" in script

    def test_it_names_the_failing_step_not_the_whole_log(self, script):
        """The first failing step identifies the failure; the rest is noise."""
        assert "step" in script
        assert "--log" not in script

    def test_a_fork_pull_request_is_still_found(self, script):
        """
        workflow_run carries `pull_requests` only for same-repo branches.

        Without the head-SHA fallback a fork's failure would be silently
        dropped, which is the one case where silence is indistinguishable
        from success.
        """
        assert "issuesAndPullRequests" in script
        assert "head_sha" in script


class TestGateLaw:
    def test_it_correctly_has_no_gate_job(self, spec):
        """
        The gate law covers `pull_request` workflows, where a missing check
        silently satisfies branch protection. This one runs on `workflow_run`,
        is never a required check, and its only job is meant to skip on a
        green run -- a gate would turn every passing CI run red.
        """
        assert "gate" not in spec["jobs"]
        triggers = spec.get("on") or spec.get(True)
        assert "pull_request" not in triggers
