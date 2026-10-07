"""
GOV-061 -- guarded Git operations need a recorded, single-use authorization.

What these tests would catch:

* a guarded operation that slips past detection because of how the shell
  command is written -- chained, wrapped in ``bash -c``/``eval``/``env``,
  given ``git -C`` options, or force-pushed with ``+refspec``;
* the opposite failure: a commit message or heredoc body that *mentions*
  "git merge" being treated as one, which would make the guard noise that
  people learn to work around;
* an authorization that can be reused, outlive its expiry, or be spent
  partially on a command that is then refused anyway.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.agent_control.git_safety import (
    DEFAULT_TTL_S,
    FORCE_PUSH,
    GitInvocation,
    authorize,
    consume,
    git_invocations,
    guarded_operations,
    operation_of,
    strip_heredocs,
)
from src.agent_control.model import (
    SCHEMA_VERSION,
    CompletionState,
    ManifestError,
    TaskPhase,
    TaskRun,
    TaskScope,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", []),
        ("git checkout -b topic", ["checkout"]),
        ("git checkout -- file && git stash pop", ["checkout", "stash"]),
        ("git reset && git reset", ["reset"]),
        ("cd a && git -C b -c core.x=y stash list", []),
        ("git stash", ["stash"]),
        ("(true)&&git reset HEAD~1", ["reset"]),
        ("(cd x; git merge y) | cat", ["merge"]),
        ("git status > out.txt 2>&1", []),
        ("git commit -m 'git merge stuff'", []),
        ('git commit -m "line one\ngit reset in a message"', []),
        ("git commit -F - <<'EOF'\ngit merge x\nEOF\ngit status", []),
        ("git commit -F - <<'EOF'\nbody\nEOF\ngit rebase main", ["rebase"]),
        ("cat <<-EOF\n\tgit merge x\n\tEOF\ngit status", []),
        ("cat <<<EOF\ngit reset", ["reset"]),
        ('echo "<<EOF"\ngit reset', ["reset"]),
        ('cat "a b" <<EOF\ngit reset\nEOF', []),
        ('echo \\"x <<EOF\ngit reset\nEOF', []),
        ("git status \\\n && git rebase main", ["rebase"]),
        ("bash -lc 'git switch main'", ["switch"]),
        ("bash script.sh", []),
        ("eval eval eval git reset", ["reset"]),
        ("eval eval eval eval git reset", []),
        ("FOO=1 timeout 10 git restore f", ["restore"]),
        ("FOO=1", []),
        ("sudo ls", []),
        ("/usr/bin/git cherry-pick abc", ["cherry-pick"]),
        ("git --version", []),
        ("git merge-base a b", []),
        ("git clean -n", ["clean"]),
        ("git push -u origin b", []),
        ("git push --force-with-lease origin b", [FORCE_PUSH]),
        ("git push --force-with-lease=b:abc origin b", [FORCE_PUSH]),
        ("git push --force-if-includes origin b", [FORCE_PUSH]),
        ("git push --force origin b", [FORCE_PUSH]),
        ("git push origin +main", [FORCE_PUSH]),
        ("git push -fu origin b", [FORCE_PUSH]),
        ('echo "unterminated && git merge x', ["merge"]),
        ('echo "unterminated && git push --force origin', [FORCE_PUSH]),
        ("python3 scripts/agent_control.py git authorize T --operation checkout", []),
    ],
)
def test_guarded_operation_detection(command: str, expected: list[str]) -> None:
    assert guarded_operations(command) == expected


def test_heredoc_bodies_are_dropped_but_the_command_line_kept() -> None:
    assert strip_heredocs("cat <<EOF\nbody\nEOF\nnext") == "cat <<EOF\nnext"


def test_invocations_carry_their_arguments() -> None:
    assert git_invocations("git -C repo log -1 && ls") == [GitInvocation("log", ("-1",))]


@pytest.mark.parametrize(
    ("invocation", "expected"),
    [
        (GitInvocation("stash", ("show", "-p")), None),
        (GitInvocation("stash", ("drop",)), "stash"),
        (GitInvocation("log", ()), None),
        (GitInvocation("push", ("origin",)), None),
    ],
)
def test_operation_of(invocation: GitInvocation, expected: str | None) -> None:
    assert operation_of(invocation) == expected


def _task() -> TaskRun:
    return TaskRun(
        schema_version=SCHEMA_VERSION,
        task_id="TB-RUN-20261007-0001",
        objective="o",
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        repository="/r/.git",
        worktree="/r",
        branch="b",
        baseline_sha="d" * 40,
        current_sha="d" * 40,
        phase=TaskPhase.IMPLEMENTING,
        status=CompletionState.ACTIVE,
        scope=TaskScope(),
    )


class TestAuthorization:
    def test_a_grant_is_recorded_with_its_checkpoint_and_expiry(self) -> None:
        task = _task()
        grant = authorize(task, "checkout", " inspect main ", checkpoint_id="CP-0002", now=NOW)
        assert grant.authorization_id == "AU-0001"
        assert grant.reason == "inspect main"
        assert grant.checkpoint_id == "CP-0002"
        assert grant.expires_at == (NOW + timedelta(seconds=DEFAULT_TTL_S)).isoformat()
        assert task.authorizations == [grant]

    @pytest.mark.parametrize(
        ("operation", "reason", "ttl", "message"),
        [
            ("commit", "r", 60, "not a guarded operation"),
            ("checkout", " ", 60, "needs a reason"),
            ("checkout", "r", 0, "ttl must be between"),
            ("checkout", "r", 3601, "ttl must be between"),
        ],
    )
    def test_invalid_grants_are_refused(
        self, operation: str, reason: str, ttl: int, message: str
    ) -> None:
        with pytest.raises(ManifestError, match=message):
            authorize(_task(), operation, reason, checkpoint_id="CP-1", now=NOW, ttl_s=ttl)

    def test_a_grant_is_spent_once(self) -> None:
        task = _task()
        authorize(task, "reset", "undo last commit", checkpoint_id="CP-1", now=NOW)
        assert consume(task, ["reset"], "git reset HEAD~1", now=NOW) == []
        assert task.authorizations[0].consumed_by == "git reset HEAD~1"
        assert task.authorizations[0].consumed_at == NOW.isoformat()
        assert consume(task, ["reset"], "git reset HEAD~1", now=NOW) == ["reset"]

    def test_an_expired_grant_does_not_count(self) -> None:
        task = _task()
        authorize(task, "merge", "r", checkpoint_id="CP-1", now=NOW, ttl_s=60)
        later = NOW + timedelta(seconds=61)
        assert consume(task, ["merge"], "git merge x", now=later) == ["merge"]

    def test_spending_is_all_or_nothing(self) -> None:
        task = _task()
        authorize(task, "checkout", "r", checkpoint_id="CP-1", now=NOW)
        missing = consume(task, ["checkout", "stash"], "git stash && git checkout x", now=NOW)
        assert missing == ["stash"]
        assert task.authorizations[0].consumed_at is None

    def test_the_recorded_command_is_redacted(self) -> None:
        task = _task()
        authorize(task, FORCE_PUSH, "r", checkpoint_id="CP-1", now=NOW)
        token = "ghp_" + "A" * 36
        command = f"git push --force https://x:{token}@github.com/o/r.git"
        assert consume(task, [FORCE_PUSH], command, now=NOW) == []
        assert token not in (task.authorizations[0].consumed_by or "")
