"""
Contract tests for the code-owned GitHub route contract.

Decides:
  - GOV-019 — CI run data is unreadable by every tool route
  - GOV-057 — every authorized tool observation crosses the repository boundary

The tool inventory here is the verified tools/list response of the connected
server. These tests assert the contract is total over that inventory, so a
tool can never be reachable by default: it is contracted or denied, never
unconsidered.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]
HOOK = PROJECT_DIR / ".claude" / "hooks" / "pre_tool_use.py"
CONTRACT = PROJECT_DIR / ".claude" / "hooks" / "_gh_contract.py"
REPO = {"owner": "zulqarnain106612-cpu", "repo": "Trade-Bot"}


def load_contract():
    spec = importlib.util.spec_from_file_location("gh_contract_under_test", CONTRACT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ghc = load_contract()


def load_hook():
    """
    The hook module, driven in-process.

    tests/test_suite_speed_budget.py holds the suite to a ratcheted
    subprocess budget, so this module imports the hook and calls main()
    rather than spawning an interpreter per decision. The decision path is
    identical: main() reads stdin, emits JSON on stdout and raises SystemExit.
    """
    spec = importlib.util.spec_from_file_location("pre_tool_use_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HOOK_MODULE = load_hook()


def decide(tool: str, tool_input: dict) -> dict:
    payload = json.dumps({"tool_name": tool, "tool_input": tool_input})
    captured = io.StringIO()
    stdin = sys.stdin
    sys.stdin = io.StringIO(payload)
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exit_info:
                HOOK_MODULE.main()
    finally:
        sys.stdin = stdin
    assert exit_info.value.code == 0
    return json.loads(captured.getvalue())["hookSpecificOutput"]


class TestInventoryIsTotal:
    def test_every_verified_tool_is_contracted_or_denied(self) -> None:
        assert ghc.unresolved_tools() == frozenset()

    def test_contract_and_denial_sets_do_not_overlap(self) -> None:
        assert not set(ghc.CONTRACTS) & set(ghc.DENIED_TOOLS)

    def test_contract_and_denial_stay_inside_the_verified_inventory(self) -> None:
        assert set(ghc.CONTRACTS) <= ghc.VERIFIED_SERVER_TOOLS
        assert set(ghc.DENIED_TOOLS) <= ghc.VERIFIED_SERVER_TOOLS

    def test_every_denial_states_a_reason(self) -> None:
        assert all(reason.strip() for reason in ghc.DENIED_TOOLS.values())

    def test_destructive_and_governance_routes_are_denied(self) -> None:
        for tool in (
            "push_files",
            "delete_file",
            "merge_pull_request",
            "create_repository",
            "fork_repository",
            "pull_request_review_write",
        ):
            assert tool in ghc.DENIED_TOOLS


class TestDeltaApplication:
    def test_unique_anchor_applies(self) -> None:
        result = ghc.apply_delta(
            "a\nb\nc\n", f"{ghc.DELTA_SENTINEL}\n<<<<<<< OLD\nb\n======= NEW\nB\n>>>>>>> END\n"
        )
        assert result.ok and result.content == "a\nB\nc\n"

    def test_ambiguous_anchor_is_refused_rather_than_guessed(self) -> None:
        result = ghc.apply_delta(
            "b\nb\n", f"{ghc.DELTA_SENTINEL}\n<<<<<<< OLD\nb\n======= NEW\nB\n>>>>>>> END\n"
        )
        assert not result.ok and "ambiguous" in result.reason

    def test_absent_anchor_is_refused(self) -> None:
        result = ghc.apply_delta(
            "a\n", f"{ghc.DELTA_SENTINEL}\n<<<<<<< OLD\nzz\n======= NEW\nB\n>>>>>>> END\n"
        )
        assert not result.ok and "not present" in result.reason

    def test_malformed_payload_is_refused(self) -> None:
        result = ghc.apply_delta("a\n", f"{ghc.DELTA_SENTINEL}\njust text\n")
        assert not result.ok and "well-formed" in result.reason

    def test_block_count_is_bounded(self) -> None:
        blocks = "".join(
            f"<<<<<<< OLD\nl{i}\n======= NEW\nL{i}\n>>>>>>> END\n"
            for i in range(ghc.HARD_MAX_DELTA_BLOCKS + 1)
        )
        base = "".join(f"l{i}\n" for i in range(ghc.HARD_MAX_DELTA_BLOCKS + 1))
        result = ghc.apply_delta(base, f"{ghc.DELTA_SENTINEL}\n{blocks}")
        assert not result.ok and "at most" in result.reason


class TestWriteBudget:
    def test_no_op_write_is_refused(self) -> None:
        ok, reason = ghc.write_within_budget("a\n", "a\n")
        assert not ok and "identical" in reason

    def test_small_edit_is_never_judged_by_ratio_alone(self) -> None:
        assert ghc.write_within_budget("a\nb\nc\n", "a\nB\nC\n")[0]

    def test_bulk_rewrite_is_refused(self) -> None:
        before = "".join(f"line{i}\n" for i in range(200))
        after = "".join(f"other{i}\n" for i in range(200))
        ok, reason = ghc.write_within_budget(before, after)
        assert not ok and "bulk-rewrite cap" in reason

    def test_single_line_change_in_a_large_file_is_allowed(self) -> None:
        before = "".join(f"line{i}\n" for i in range(200))
        assert ghc.write_within_budget(before, before.replace("line7", "line7_fixed"))[0]


@pytest.fixture
def seeded_blob():
    """A fetched body on disk, as PostToolUse would have recorded it."""
    store = ghc.BlobStore(PROJECT_DIR / ".claude" / ".hook-state" / "gh_blobs")
    path = "tests/_contract_fixture.py"
    body = "def value():\n    return 1\n" + "".join(f"# pad {i}\n" for i in range(40))
    store.put(path, "contract/fixture", body, sha="fixturesha")
    yield path, body
    target = store._key(path, "contract/fixture")
    if target.exists():
        target.unlink()


class TestWriteRouteEndToEnd:
    def test_delta_write_is_reconstructed_into_a_full_blob(self, seeded_blob) -> None:
        path, body = seeded_blob
        decision = decide(
            "mcp__github__create_or_update_file",
            {
                **REPO,
                "path": path,
                "branch": "contract/fixture",
                "message": "fix value",
                "content": (
                    f"{ghc.DELTA_SENTINEL}\n<<<<<<< OLD\n    return 1\n"
                    "======= NEW\n    return 2\n>>>>>>> END\n"
                ),
            },
        )
        assert decision["permissionDecision"] == "allow"
        updated = decision["updatedInput"]
        # The agent sent four lines; GitHub receives the whole file.
        assert updated["content"] == body.replace("return 1", "return 2")
        assert updated["sha"] == "fixturesha"

    def test_delta_without_a_fetched_body_is_refused(self) -> None:
        decision = decide(
            "mcp__github__create_or_update_file",
            {
                **REPO,
                "path": "tests/_never_fetched.py",
                "branch": "contract/fixture",
                "message": "m",
                "content": f"{ghc.DELTA_SENTINEL}\n<<<<<<< OLD\na\n======= NEW\nb\n>>>>>>> END\n",
            },
        )
        assert decision["permissionDecision"] == "deny"
        assert "get_file_contents" in decision["permissionDecisionReason"]

    def test_bulk_rewrite_of_a_fetched_file_is_refused(self, seeded_blob) -> None:
        path, _ = seeded_blob
        decision = decide(
            "mcp__github__create_or_update_file",
            {
                **REPO,
                "path": path,
                "branch": "contract/fixture",
                "message": "rewrite",
                "content": "".join(f"new{i}\n" for i in range(42)),
            },
        )
        assert decision["permissionDecision"] == "deny"
        assert "bulk-rewrite cap" in decision["permissionDecisionReason"]

    def test_sha_for_an_unfetched_file_is_refused(self) -> None:
        decision = decide(
            "mcp__github__create_or_update_file",
            {
                **REPO,
                "path": "tests/_never_fetched_2.py",
                "branch": "contract/fixture",
                "message": "m",
                "content": "x\n",
                "sha": "a" * 40,
            },
        )
        assert decision["permissionDecision"] == "deny"
        assert "never fetched" in decision["permissionDecisionReason"]


class _FakeVerdict:
    """Stands in for scripts/ci_verdict.py so tests never reach the network."""

    class VerdictError(RuntimeError):
        pass

    answer = "merged successfully"
    seen_hint: object = "unset"

    @staticmethod
    def has_credentials() -> bool:
        return True

    @classmethod
    def verdict(cls, hint=None):
        cls.seen_hint = hint
        if isinstance(cls.answer, Exception):
            raise cls.answer
        return cls.answer


@pytest.fixture
def fake_verdict(monkeypatch):
    monkeypatch.setitem(sys.modules, "ci_verdict", _FakeVerdict)
    _FakeVerdict.answer = "merged successfully"
    _FakeVerdict.seen_hint = "unset"
    return _FakeVerdict


class TestCiRequestsReceiveOnlyTheVerdict:
    """
    Whatever the agent executes, a CI request is answered with the verdict and
    never with the record behind it. The reason text is the whole answer, so
    these assert equality rather than containment.
    """

    @pytest.mark.parametrize(
        ("tool", "tool_input"),
        [
            ("mcp__github__pull_request_read", {**REPO, "method": "get_status", "pullNumber": 7}),
            (
                "mcp__github__pull_request_read",
                {**REPO, "method": "get_check_runs", "pullNumber": 7},
            ),
            ("mcp__github__get_workflow_run_logs", {**REPO, "run_id": 1}),
            ("mcp__github__list_workflow_run_artifacts", {**REPO, "run_id": 1}),
            ("Bash", {"command": "gh run view --log"}),
            ("Bash", {"command": "gh pr checks 7"}),
            ("Bash", {"command": "curl https://api.github.com/repos/o/r/actions/runs/1"}),
            ("Bash", {"command": "tail -f logs/app.log"}),
        ],
    )
    def test_every_ci_route_returns_the_verdict_and_nothing_else(
        self, fake_verdict, tool, tool_input
    ) -> None:
        decision = decide(tool, tool_input)
        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"] == "merged successfully"

    def test_failure_lines_are_passed_through_verbatim(self, fake_verdict) -> None:
        lines = "CI / shard 5: FAILED tests/test_x.py::test_y\nCI / lint: error: bad import"
        fake_verdict.answer = lines
        decision = decide(
            "mcp__github__pull_request_read", {**REPO, "method": "get_status", "pullNumber": 7}
        )
        assert decision["permissionDecisionReason"] == lines

    def test_the_pull_request_number_is_carried_into_the_verdict(self, fake_verdict) -> None:
        decide("mcp__github__pull_request_read", {**REPO, "method": "get_status", "pullNumber": 41})
        assert fake_verdict.seen_hint == 41

    def test_a_pull_request_url_also_identifies_the_target(self, fake_verdict) -> None:
        decide(
            "mcp__github__get_workflow_run_logs",
            {**REPO, "url": "https://api.github.com/repos/o/r/pulls/88/checks"},
        )
        assert fake_verdict.seen_hint == 88

    def test_a_broken_verdict_never_falls_through_to_raw_data(self, fake_verdict) -> None:
        fake_verdict.answer = _FakeVerdict.VerdictError("GitHub API 500")
        decision = decide(
            "mcp__github__pull_request_read", {**REPO, "method": "get_status", "pullNumber": 7}
        )
        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"].startswith("ci verdict unavailable")

    def test_the_exact_pr_report_command_keeps_its_own_route(self, fake_verdict) -> None:
        decision = decide("Bash", {"command": "./scripts/prfail --pr 458"})
        assert decision["permissionDecision"] == "allow"

    def test_an_ordinary_read_is_not_a_ci_request(self, fake_verdict) -> None:
        decision = decide(
            "mcp__github__get_file_contents",
            {**REPO, "path": "CLAUDE.md", "ref": "main", "tb_lines": "1-5"},
        )
        assert decision["permissionDecision"] == "allow"


class TestReadsReturnTheExactBlock:
    """
    A read names its target and receives that block, with real line numbers.
    A whole file is not an answer to a question about a few lines.
    """

    def test_a_read_without_a_target_is_refused(self) -> None:
        decision = decide(
            "mcp__github__get_file_contents", {**REPO, "path": "CLAUDE.md", "ref": "main"}
        )
        assert decision["permissionDecision"] == "deny"
        assert "must name its target" in decision["permissionDecisionReason"]

    def test_a_target_may_not_be_given_twice(self) -> None:
        decision = decide(
            "mcp__github__get_file_contents",
            {**REPO, "path": "a.py", "ref": "main", "tb_lines": "1-2", "tb_anchor": "def x"},
        )
        assert decision["permissionDecision"] == "deny"

    def test_the_target_is_stripped_from_the_executed_call(self) -> None:
        decision = decide(
            "mcp__github__get_file_contents",
            {**REPO, "path": "a.py", "ref": "main", "tb_anchor": "def enforce_live_gate"},
        )
        assert decision["permissionDecision"] == "allow"
        assert set(decision["updatedInput"]) == {"owner", "repo", "path", "ref"}

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ({"tb_lines": "3-5"}, "3| c\n4| d\n5| e"),
            ({"tb_lines": "2"}, "2| b"),
            ({"tb_anchor": "d"}, "1| a\n2| b\n3| c\n4| d\n5| e"),
        ],
    )
    def test_only_the_named_block_comes_back_with_line_numbers(self, target, expected) -> None:
        body = "a\nb\nc\nd\ne\n"
        block, problem = ghc.slice_to_target(body, target)
        assert not problem
        assert block == expected

    def test_an_ambiguous_anchor_returns_no_block(self) -> None:
        block, problem = ghc.slice_to_target("x\nx\n", {"tb_anchor": "x"})
        assert not block and "make it unique" in problem

    def test_a_target_range_is_bounded(self) -> None:
        body = "".join(f"line{i}\n" for i in range(500))
        block, problem = ghc.slice_to_target(body, {"tb_lines": "1-500"})
        assert not problem
        assert len(block.splitlines()) == ghc.HARD_MAX_TARGET_LINES


class TestCiUrlsAndMethodsAreAnsweredNotLeaked:
    """
    These moved here from the subprocess-driven module: the verdict is faked,
    so asserting them never calls GitHub.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.github.com/repos/zulqarnain106612-cpu/Trade-Bot/actions/runs/1",
            "https://api.github.com/repos/zulqarnain106612-cpu/Trade-Bot/check-runs/1",
            "https://api.github.com/repos/zulqarnain106612-cpu/Trade-Bot/commits/abc/status",
            "https://api.github.com/repos/zulqarnain106612-cpu/Trade-Bot/actions/artifacts",
        ],
    )
    def test_a_ci_url_on_an_allowed_tool_returns_the_verdict(self, fake_verdict, url) -> None:
        decision = decide(
            "mcp__github__get_file_contents",
            {**REPO, "path": "a.py", "ref": "main", "tb_lines": "1-5", "url": url},
        )
        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"] == "merged successfully"

    def test_without_a_credential_there_is_no_verdict_and_the_route_still_denies(
        self, monkeypatch
    ) -> None:
        class NoCreds:
            class VerdictError(RuntimeError):
                pass

            @staticmethod
            def has_credentials() -> bool:
                return False

            @staticmethod
            def verdict(hint=None):  # pragma: no cover - must never be reached
                raise AssertionError("verdict attempted without a credential")

        monkeypatch.setitem(sys.modules, "ci_verdict", NoCreds)
        decision = decide(
            "mcp__github__pull_request_read", {**REPO, "method": "get_status", "pullNumber": 7}
        )
        assert decision["permissionDecision"] == "deny"
        assert "get_status" in decision["permissionDecisionReason"]
