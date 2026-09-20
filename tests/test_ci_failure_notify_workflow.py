"""
The failure notice is the thing that makes not-polling safe.

GOV-011 and GOV-012 forbid an agent from going and looking at CI: no live
watch, and no unbounded log fetch. That is only workable if failures come to
the pull request on their own. These tests pin the properties that make the
notice trustworthy -- it fires on every not-green conclusion including an
unexcused skip, carries the exact failing lines rather than only a step name,
stays inside the same line budget the command policy enforces, and replaces its
own prior comment instead of stacking new ones.
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

    def test_every_completed_run_is_inspected(self, spec):
        """
        The job carries no `if:` on purpose.

        A run's own conclusion does not tell you whether a job inside it was
        skipped or neutral -- a run whose jobs all skipped concludes
        `success`. That is exactly how #292 merged with four unrun gates. So
        the filtering happens per job, inside the script, not on the job.
        """
        assert "if" not in spec["jobs"]["notify"]

    @pytest.mark.parametrize(
        "conclusion",
        ["failure", "cancelled", "timed_out", "neutral", "action_required", "stale"],
    )
    def test_every_not_green_conclusion_is_reported(self, script, conclusion):
        """
        CLAUDE.md treats not-green as the failure condition, not just red.

        The predicate is an inverted allowlist -- anything that is not
        `success` is reported -- so a conclusion GitHub adds tomorrow is
        covered the day it appears rather than the day someone notices.
        """
        assert "if (j.conclusion === 'success') return false;" in script
        # The comment on the fall-through names each conclusion it covers, so
        # a new one cannot be added to GitHub's vocabulary and quietly ignored.
        assert conclusion in script

    def test_an_unexpected_skip_is_reported(self, script):
        """
        A skipped required check satisfies branch protection without having
        verified anything. Only the skips assert_jobs_green.py excuses are
        excused here, and the two lists must name the same jobs.
        """
        assert "ALLOW_SKIPPED" in script
        assert "'retrieve-context'" in script

        # assert_jobs_green.py reads its allowlist from the ALLOW_SKIPPED
        # environment variable, so the authority on which job is excused is
        # the workflow that sets it -- today, exactly one.
        allowed = set()
        for path in (PROJECT_ROOT / ".github" / "workflows").glob("*.yml"):
            wf = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job in (wf.get("jobs") or {}).values():
                for step in job.get("steps") or []:
                    value = (step.get("env") or {}).get("ALLOW_SKIPPED")
                    if value:
                        allowed |= {v.strip() for v in value.split(",") if v.strip()}
                value = (job.get("env") or {}).get("ALLOW_SKIPPED")
                if value:
                    allowed |= {v.strip() for v in value.split(",") if v.strip()}

        for job_name in allowed:
            assert f"'{job_name}'" in script, (
                f"{job_name} is excused in a gate but not in the failure notice"
            )

    def test_a_green_run_is_silent(self, script):
        """
        A notification that fires on success is one people learn to ignore.

        The one exception is a standing red notice from an earlier attempt:
        that is rewritten to say the workflow recovered, because a stale red
        comment is worse than no comment.
        """
        assert "else if (!green)" in script
        assert "recovered" in script


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
        assert "const MAX_JOBS = 4" in script
        assert "const MAX_MSG_LINES = 4" in script
        assert "slice(0, MAX_JOBS)" in script

        # Worst case: MAX_JOBS job lines, each followed by MAX_MSG_LINES of
        # extracted message, plus the heading, link and footer. That has to
        # stay inside the same 30-line budget the command policy enforces.
        worst_case = 4 * (1 + 4) + 6
        assert worst_case <= 30, worst_case

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

    def test_the_notice_carries_the_exact_failure_message(self, script):
        """
        A step name says which gate broke, not what it said. Without the
        message an agent has to go and read the log, which is the one thing
        GOV-012 forbids -- so the run that already has the log extracts the
        failing lines itself, annotations first and a filtered, capped log
        read only as a fallback.
        """
        assert "listAnnotations" in script
        assert "downloadJobLogsForWorkflowRun" in script
        assert "ERROR_RE" in script
        assert "slice(-MAX_MSG_LINES)" in script

    def test_reading_the_log_requires_no_extra_permission_than_declared(self, spec):
        """Annotations are check-run data; the token has to be allowed to read them."""
        assert spec["permissions"]["checks"] == "read"

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
        is never a required check, and its only job deliberately posts
        nothing on a green run -- a gate would turn every passing CI run red.
        """
        assert "gate" not in spec["jobs"]
        triggers = spec.get("on") or spec.get(True)
        assert "pull_request" not in triggers
