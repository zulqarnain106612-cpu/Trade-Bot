"""
CI run data is unreadable from a session, and the comment channel is not.

The previous policy capped CI log retrieval instead of banning it: "the
minimum number of lines that explains the failure is always permitted". That
bound was the loophole. The minimum number of lines is a judgement made by the
party that wants the lines, and it decayed into paging logs a few lines at a
time -- which is the context cost the cap existed to prevent, paid in
instalments.

So the rule is now unconditional, and these tests pin the two halves that make
an unconditional rule survive contact with use:

**Nothing gets through.** Not with a bound, not with a filter, not through a
different verb, not by dispatching a run and reading what it prints, and not
via a check-results flag on a subcommand whose comment form is allowed.

**The comment channel stays open.** With logs unreadable, the notice comment is
the only way to learn why a run failed. A guard that closed it too would leave
no route at all, and a guard with no route is a guard the next session turns
off -- so the allowlist is part of the control rather than an exception to it.

The third half is that the two enforcement points agree. The PreToolUse hook
imports the shared implementation when the project is importable and falls back
to the policy file's patterns when it is not, so the same command must be
refused either way.

Decides:
  - GOV-019 — CI run data is unreadable from a session under every condition
  - GOV-020 — One comment per commit carries the status and the exact failing lines
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from common.command_schema import (
    CI_COMMENT_ALLOW_PATTERNS,
    CI_HARD_DENY_PATTERNS,
    CI_LOG_PATTERNS,
    CI_LOG_REFUSAL,
    is_ci_log_access,
)
from common.shell_exec import run

PROJECT_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = PROJECT_ROOT / "config" / "command_policy.json"


@pytest.fixture(scope="module")
def policy() -> dict:
    """Module scope: it parses a file and every test treats it as read-only."""
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))["ci_log_access"]


# Every shape of "read CI run data" this project has seen or can foresee.
# The first four were *permitted* before, under the bounded exemption.
BLOCKED = [
    "gh run view 1 --log | head -20",
    "gh run view --job 1 --log-failed | grep -m 5 error",
    "gh run list --limit 10",
    "gh pr checks 313",
    "gh run view 1 --log",
    "gh run view 1",
    "gh run list",
    "gh run download 7",
    "gh run rerun 7",
    "gh run cancel 7",
    "gh workflow run ci.yml",
    "gh workflow view ci.yml",
    "gh workflow list",
    "gh cache list",
    "gh cache delete abc",
    "gh api repos/o/r/actions/runs/1",
    "gh api repos/o/r/actions/runs/1/jobs",
    "gh api repos/o/r/actions/artifacts",
    "gh api repos/o/r/check-runs/5",
    "gh api repos/o/r/check-suites/5",
    "gh api repos/o/r/commits/abc123/check-runs",
    "gh api repos/o/r/commits/abc123/status",
    "gh api repos/o/r/statuses/abc123",
    "gh pr view 9 --json statusCheckRollup",
    "gh pr view 9 --json number,statusCheckRollup,title",
    "gh pr view 9 --json checkRuns",
    "gh run view 9 --json jobs",
    "curl -s https://api.github.com/repos/o/r/actions/runs/1/logs",
    "curl https://api.github.com/repos/o/r/actions/jobs/2/logs -o log.zip",
    "curl -s https://api.github.com/repos/o/r/check-runs/3",
    "act -j test",
]

# The sanctioned channel, plus ordinary pull-request work that must not be
# swept up by a broad `gh api` rule.
ALLOWED = [
    "gh pr view 313 --json comments",
    "gh pr view 313 --json comments --jq '.comments[-1].body'",
    "gh pr comment 313 --body 'pushed a fix'",
    "gh issue view 42",
    "gh issue comment 42 --body hi",
    "gh api repos/o/r/issues/313/comments",
    "gh api repos/o/r/issues/comments/99",
    "gh api repos/o/r/pulls/313/comments",
    "gh pr view 313 --json state,title,mergeStateStatus",
    "gh pr merge 313 --squash --auto",
    "gh pr create --base main --title x --body y",
    "gh pr list --state open",
    "git log --oneline -1",
    "ls .github/workflows/",
    "grep -n 'runs-on' .github/workflows/ci.yml",
]


class TestNothingGetsThrough:
    @pytest.mark.parametrize("command", BLOCKED, ids=BLOCKED)
    def test_the_shared_check_refuses_it(self, command: str) -> None:
        assert is_ci_log_access(command)

    @pytest.mark.parametrize(
        "suffix",
        [
            "",
            " | head -1",
            " | head -5",
            " | tail -3",
            " | grep -m 1 error",
            " | wc -l",
            " > /tmp/out.txt",
            " 2>/dev/null | head -2",
        ],
    )
    def test_no_bound_or_filter_makes_it_allowed(self, suffix: str) -> None:
        """
        The loophole, closed. Each suffix is a way of saying "but only a few
        lines", which is precisely what the old policy accepted and what turned
        a cap into a paging loop. The objection is not output size.
        """
        assert is_ci_log_access(f"gh run view 1 --log{suffix}")

    def test_a_run_dispatched_in_order_to_read_it_is_the_same_act(self) -> None:
        """
        `gh workflow run` and `gh run rerun` produce nothing to read by
        themselves -- they are here because triggering a run and then reading
        what it prints is the same thing with an extra step, and leaving them
        open leaves the route open.
        """
        assert is_ci_log_access("gh workflow run ci.yml")
        assert is_ci_log_access("gh run rerun 7")

    @pytest.mark.parametrize("command", ["gh pr checks 9", "gh pr view 9 --json statusCheckRollup"])
    def test_check_results_beat_the_comment_allowlist(self, command: str) -> None:
        """
        The tier that exists because two rules share a subcommand.

        `gh pr view --json statusCheckRollup` is one flag away from
        `gh pr view --json comments`; a flat allowlist would wave it through.
        Check results are CI data whatever verb fetched them.
        """
        assert is_ci_log_access(command)


class TestTheCommentChannelStaysOpen:
    @pytest.mark.parametrize("command", ALLOWED, ids=ALLOWED)
    def test_it_is_not_treated_as_ci_access(self, command: str) -> None:
        assert not is_ci_log_access(command)

    def test_the_refusal_names_the_remaining_route(self) -> None:
        """
        A refusal with no alternative is a dead end, and a dead end is what
        makes the next session set TB_COMMAND_POLICY=off. The message has to
        say where the answer actually is.
        """
        assert "comment" in CI_LOG_REFUSAL.lower()
        assert "gh pr view" in CI_LOG_REFUSAL
        # And it must not suggest a smaller read would work.
        assert "30 lines" not in CI_LOG_REFUSAL


class TestTheRuntimeRefusesToo:
    """
    Routing a command through shell_exec.run() must not be a way around the
    hook. It is the sanctioned way to run anything, so it is the obvious place
    to try when a direct Bash call is refused.
    """

    @pytest.mark.parametrize("command", ["gh run view 1 --log", "gh pr checks 9"])
    def test_run_refuses_without_executing(self, command: str) -> None:
        result = run({"command": command, "output_policy": {"max_lines": 1}})
        assert result["exit_code"] == -1
        assert result["attempt_count"] == 0, "the command must not have run"
        assert "permanently unreadable" in (result["error"] or "")

    def test_run_still_allows_reading_a_comment(self) -> None:
        """
        Not executed for real -- `gh` may be absent and this must not touch the
        network. What is asserted is that the declaration is not *refused*:
        attempt_count of 1 means the guard let it reach execution.
        """
        result = run(
            {
                "command": "true gh pr view 1 --json comments",
                "output_policy": {"max_lines": 1},
            }
        )
        assert result["attempt_count"] == 1
        assert "permanently unreadable" not in (result["error"] or "")


class TestEnforcementPointsAgree:
    def test_the_policy_file_mirrors_the_shared_patterns(self, policy: dict) -> None:
        """
        The hook falls back to the policy file when the project is not
        importable -- a bare session, a detached checkout. If the two lists
        drifted, the block would hold in one kind of session and not the other,
        and nobody would notice which.
        """
        assert tuple(policy["banned_patterns"]) == CI_LOG_PATTERNS
        assert tuple(policy["hard_deny_patterns"]) == CI_HARD_DENY_PATTERNS
        assert tuple(policy["allowed_patterns"]) == CI_COMMENT_ALLOW_PATTERNS

    def test_the_policy_is_enabled(self, policy: dict) -> None:
        assert policy["enabled"] is True

    def test_every_policy_pattern_compiles(self, policy: dict) -> None:
        """An invalid regex here fails open, silently allowing everything."""
        for key in ("banned_patterns", "hard_deny_patterns", "allowed_patterns"):
            for pattern in policy[key]:
                re.compile(pattern)

    def test_the_policy_message_matches_the_shared_refusal(self, policy: dict) -> None:
        assert policy["message"] == CI_LOG_REFUSAL

    def test_the_fallback_path_reaches_the_same_verdict(self, policy: dict) -> None:
        """
        Exercises the policy patterns directly, the way the hook does with no
        project import, and requires the same answer as the shared function for
        every case in both corpora.
        """

        def by_policy(command: str) -> bool:
            for pattern in policy["hard_deny_patterns"]:
                if re.search(pattern, command, re.IGNORECASE):
                    return True
            for pattern in policy["allowed_patterns"]:
                if re.search(pattern, command, re.IGNORECASE):
                    return False
            return any(
                re.search(pattern, command, re.IGNORECASE) for pattern in policy["banned_patterns"]
            )

        for command in BLOCKED + ALLOWED:
            assert by_policy(command) == is_ci_log_access(command), command


class TestTheNoticeIsTheOnlyChannel:
    def test_the_notice_workflow_exists(self) -> None:
        """
        The rule and the notice are one decision. Blocking log access without
        a notice that carries the failure leaves a contributor with no way to
        learn why their pull request is red.
        """
        assert (PROJECT_ROOT / ".github" / "workflows" / "ci-failure-notify.yml").is_file()

    def test_the_policy_points_at_it(self) -> None:
        note = json.loads(POLICY_PATH.read_text(encoding="utf-8"))["ci_log_access"]["_note"]
        assert "ci-failure-notify.yml" in note
