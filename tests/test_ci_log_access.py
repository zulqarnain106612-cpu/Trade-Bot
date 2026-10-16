"""
CI run data is not a model-observation route. The sole diagnostic route is the
exact ./scripts/prfail --pr <PR_NUMBER> invocation, which reads the finalized
filtered Check Run report and never downloads logs or falls back to the
historical PR-comment notice.

The command-policy classifier and JSON fallback must agree. The active
pre-execution hook is a separate runtime layer; this checkout has pre-existing
deletions of that hook and its settings, so these tests do not claim host-wide
enforcement.

Decides:
  - GOV-019 — direct CI run data remains inaccessible; the filtered report is the sole CLI route
  - GOV-020 — terminal workflow outcomes are consolidated into one SHA-bound report
  - GOV-057 — all model-visible tool observations cross the repository boundary
<<<<<<< HEAD
  - REG-0024 — local checks are CI-only and the prepare route exposes only the current HEAD notice
=======
  - REG-0024 — local checks are CI-only and the legacy prepare route is retired
>>>>>>> origin/main
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
    is_exact_patch_apply_command,
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
    "gh pr view 313 --json comments",
    "gh pr view 313 --json comments --jq '.comments[-1].body'",
    "gh issue view 42 --json comments",
    "gh api repos/o/r/issues/313/comments",
    "gh api repos/o/r/issues/comments/99",
    "gh api repos/o/r/pulls/313/comments",
    'gh api graphql -f query=\'{ repository(owner: "o", name: "r") { issue(number: 313) { comments(first: 100) { nodes { body } } } } }\'',
    "curl -s https://api.github.com/graphql",
    "python3 -c 'import urllib.request; urllib.request.urlopen(\"https://api.github.com/repos/o/r/issues/313/comments\")'",
    "gh run view 9 --json jobs",
    "python3 scripts/ci_verdict.py",
    "python3 scripts/ci_diagnostics.py collect",
    "python3 -m scripts.ci_diagnostics collect",
    "python3 -c 'from scripts.ci_diagnostics import _collect; _collect()'",
    "python3 - <<'PY'\nfrom scripts import ci_diagnostics\nci_diagnostics._collect()\nPY",
    "python3 scripts/local_checks.py prepare",
    "./scripts/prfail --pr 0",
    "./scripts/prfail --pr 313 --run-id 9",
    "cd /tmp && ./scripts/prfail --pr 313",
    "./scripts/prfail --pr 313 | cat",
    "curl -s https://api.github.com/repos/o/r/actions/runs/1/logs",
    "curl https://api.github.com/repos/o/r/actions/jobs/2/logs -o log.zip",
    "curl -s https://api.github.com/repos/o/r/check-runs/3",
    "act -j test",
]

# The sanctioned channel, plus ordinary pull-request work that must not be
# swept up by a broad `gh api` rule.
ALLOWED = [
<<<<<<< HEAD
=======
    "./scripts/prfail --pr 313",
>>>>>>> origin/main
    "gh pr comment 313 --body 'pushed a fix'",
    "gh issue view 42",
    "gh issue comment 42 --body hi",
    "gh pr view 313 --json state,title,mergeStateStatus",
    "python3 scripts/local_checks.py prepare",
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


class TestTheCanonicalReportCommandStaysOpen:
    @pytest.mark.parametrize("command", ALLOWED, ids=ALLOWED)
    def test_it_is_not_treated_as_ci_access(self, command: str) -> None:
        assert not is_ci_log_access(command)

    def test_only_the_quoted_report_bound_patch_heredoc_is_exempt(self) -> None:
        command = "\n".join(
            [
                "python3 scripts/apply_ci_patch.py --pr 458 --head-sha "
                + "a" * 40
                + " --patch-stdin <<'TB_CI_PATCH'",
                "diff --git a/src/example.py b/src/example.py",
                "--- a/src/example.py",
                "+++ b/src/example.py",
                "@@ -1,1 +1,1 @@",
                "-before = 1",
                "+after = 1",
                "TB_CI_PATCH",
            ]
        )
        assert is_exact_patch_apply_command(command)
        assert not is_ci_log_access(command)
        assert not is_exact_patch_apply_command("cd /tmp && " + command)

    def test_the_refusal_names_the_remaining_route(self) -> None:
        """
        A refusal with no alternative is a dead end, and a dead end is what
        makes the next session set TB_COMMAND_POLICY=off. The message has to
        say where the answer actually is.
        """
<<<<<<< HEAD
        assert "comment" in CI_LOG_REFUSAL.lower()
        assert "local_checks.py prepare" in CI_LOG_REFUSAL
=======
        assert CI_LOG_REFUSAL == (
            "THAPPAR — CI DIAGNOSTIC ACCESS BLOCKED. Use only: "
            "./scripts/prfail --pr <PR_NUMBER>. "
            "Direct or alternative diagnostic retrieval is prohibited."
        )
        assert "./scripts/prfail --pr <PR_NUMBER>" in CI_LOG_REFUSAL
>>>>>>> origin/main
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
        assert "blocked" in (result["error"] or "").lower()

<<<<<<< HEAD
=======
    def test_noncanonical_patch_applier_invocations_are_refused(self) -> None:
        from common.command_schema import is_local_check_execution

        assert is_local_check_execution("python3 scripts/apply_ci_patch.py --help")
        assert is_local_check_execution(
            "python3 scripts/apply_ci_patch.py --pr 1 --head-sha " + "a" * 40
        )

>>>>>>> origin/main
    def test_run_refuses_arbitrary_comment_fetches(self) -> None:
        result = run(
            {
                "command": "gh pr view 1 --json comments",
                "output_policy": {"max_lines": 1},
            }
        )
        assert result["exit_code"] == -1
        assert result["attempt_count"] == 0


class TestEnforcementPointsAgree:
    def test_the_policy_file_mirrors_the_shared_patterns(self, policy: dict) -> None:
        """
        The active hook may need the policy file when the project is not importable.
        The shared classifier and fallback patterns must remain identical.
        """
        assert tuple(policy["banned_patterns"]) == CI_LOG_PATTERNS
        assert tuple(policy["hard_deny_patterns"]) == CI_HARD_DENY_PATTERNS
        assert tuple(policy["allowed_patterns"]) == CI_COMMENT_ALLOW_PATTERNS

    def test_the_policy_is_enabled(self, policy: dict) -> None:
        assert policy["enabled"] is True

<<<<<<< HEAD
    def test_only_current_notice_preparation_is_allowed(self) -> None:
        assert not is_ci_log_access("python3 scripts/local_checks.py prepare")
=======
    def test_only_the_canonical_report_command_is_allowed(self) -> None:
        assert not is_ci_log_access("./scripts/prfail --pr 313")
        assert is_ci_log_access("python3 scripts/local_checks.py prepare")
>>>>>>> origin/main
        assert is_ci_log_access("gh pr view 313 --json comments")
        assert is_ci_log_access("gh api repos/o/r/issues/313/comments")

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


class TestTheFilteredReportIsTheOnlyAgentChannel:
    def test_the_diagnostic_collector_workflow_exists(self) -> None:
        """
        The report interface and policy are one decision. A collector that is not
        wired to the retrieval command cannot give a contributor actionable
        diagnostics.
        """
        assert (PROJECT_ROOT / ".github" / "workflows" / "ci-diagnostics.yml").is_file()

    def test_the_policy_points_at_the_collector(self) -> None:
        note = json.loads(POLICY_PATH.read_text(encoding="utf-8"))["ci_log_access"]["_note"]
        assert "ci-diagnostics.yml" in note
