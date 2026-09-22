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
import re
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

    def test_it_waits_for_every_watched_workflow_before_posting(self, script):
        """
        Replaces `test_a_green_run_is_silent`, deliberately.

        That test protected against notification fatigue -- a notice that fires
        on success gets ignored -- and it was right while CI logs were still
        readable with a line bound, because a silent green run left the log as
        the fallback. `ci_log_access` removed that fallback: this comment is now
        the only channel, so "all checks green" has to be sayable, and the
        answer to fatigue is one comment per commit rather than no comment.

        What must hold instead is that the verdict is never posted early. A
        notice saying "all green" while a workflow is still in flight is worse
        than fatigue: it is wrong, and it is the only thing anyone can read.
        """
        assert "status !== 'completed'" in script
        assert "Not posting yet" in script
        assert "return;" in script

    def test_the_verdict_covers_every_watched_workflow(self, script, spec):
        """
        The consolidation list and the trigger list are one fact.

        Adding a workflow to `on.workflow_run.workflows` without adding it to
        WATCHED would let the notice declare a complete picture while ignoring
        that workflow's result -- announcing green on a commit whose new
        workflow failed.
        """
        # PyYAML parses a bare `on:` key as the boolean True.
        on = spec.get("on") or spec.get(True)
        triggers = set(on["workflow_run"]["workflows"])
        watched = set(re.findall(r"^\s*'([^']+)',$", script, re.MULTILINE))
        assert triggers <= watched, f"not consolidated: {sorted(triggers - watched)}"

    def test_green_is_reported_not_suppressed(self, script):
        """The only channel must be able to say 'nothing is wrong'."""
        assert "all checks green" in script


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
        assert "const MAX_JOBS = 6" in script
        assert "const MAX_MSG_LINES = 4" in script
        assert "slice(0, MAX_JOBS)" in script

        # Worst case, with the consolidated shape: MAX_JOBS entries, each a
        # heading line plus a fenced block of at most MAX_MSG_LINES, plus the
        # status heading, a blank and the overflow line. MAX_JOBS rose from 4
        # to 6 because one comment now covers five workflows instead of one --
        # the budget is per comment, and there is only one comment, so the cap
        # had to cover the same failures it used to spread across five.
        worst_case = 6 * (1 + 2 + 4) + 3
        assert worst_case <= 48, worst_case
        # No footer: the comment is the status and the errors, nothing else.
        assert "Do not fetch the run log" not in script

        policy = json.loads(
            (PROJECT_ROOT / "config" / "command_policy.json").read_text(encoding="utf-8")
        )
        assert policy["bounded_output"]["max_declared_lines"] >= 20

    def test_it_updates_its_prior_notice_rather_than_stacking(self, script):
        assert "updateComment" in script
        # One marker for the whole commit, not one per workflow. The old
        # `ci-failure-notice:${run.name}` marker was per-workflow by design and
        # produced up to five comments on one PR; the requirement is now a
        # single consolidated comment, so the marker carries no workflow name.
        assert "'<!-- ci-notice -->'" in script
        assert "ci-failure-notice:" not in script

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
        assert "slice(-MAX_MSG_LINES)" in script
        # Replaces the single ERROR_RE. A prioritised SIGNAL list is needed
        # because one alternation cannot express "prefer the pytest summary
        # line over a bare traceback frame", and the flat regex is what let
        # `Process completed with exit code 1` become the entire notice.
        assert "const SIGNAL = [" in script
        assert "const NOISE = [" in script

    def test_the_runners_exit_code_is_not_treated_as_an_error_message(self, script):
        """
        The failure this test exists for, observed on PR #313: the notice's
        only content was `Process completed with exit code 1.` -- which names
        no assertion, no test and no file. With CI logs now permanently
        unreadable, a notice that says only that leaves nobody any route to
        the cause at all.
        """
        assert "Process completed with exit code" in script
        assert "isNoise" in script
        assert "filter(l => !isNoise(l))" in script

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
