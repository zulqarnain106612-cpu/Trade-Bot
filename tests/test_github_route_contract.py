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

import importlib.util
import json
import os
import subprocess
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


def decide(tool: str, tool_input: dict) -> dict:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(PROJECT_DIR)}
    env.pop("TB_COMMAND_POLICY", None)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0
    return json.loads(proc.stdout)["hookSpecificOutput"]


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
