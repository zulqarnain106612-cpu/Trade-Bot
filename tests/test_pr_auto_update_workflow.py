"""
The last manual step in the merge path.

`main` requires an up-to-date branch, so every merge leaves every other open
pull request one commit behind and waiting for somebody to press "Update
branch". These tests pin the three properties that make pressing it
automatically safe rather than chaotic: it updates exactly one pull request per
merge, it refuses to run with a token whose pushes start no workflow, and it
touches only branches that are genuinely stale.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "pr-auto-update.yml"


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def step(spec: dict) -> dict:
    steps = spec["jobs"]["update-next"]["steps"]
    (only,) = [s for s in steps if "github-script" in s.get("uses", "")]
    return only


@pytest.fixture(scope="module")
def script(step: dict) -> str:
    return step["with"]["script"]


class TestTrigger:
    def test_it_runs_when_main_moves(self, spec):
        """A merge is the event that makes every other branch stale."""
        triggers = spec.get("on") or spec.get(True)
        assert triggers["push"]["branches"] == ["main"]

    def test_it_can_be_restarted_by_hand(self, spec):
        """
        The chain is self-sustaining only while it is running. If it stops --
        the workflow disabled, a merge made while it was -- there has to be a
        way to start it again that is not another merge.
        """
        triggers = spec.get("on") or spec.get(True)
        assert "workflow_dispatch" in triggers

    def test_two_runs_cannot_pick_the_same_pull_request(self, spec):
        """
        Both would read "the oldest behind pull request" and both would update
        it, which is a wasted CI run at best.
        """
        assert spec["concurrency"]["group"] == "pr-auto-update"
        assert spec["concurrency"]["cancel-in-progress"] is False


class TestItRefusesTheTokenThatWouldBreakThings:
    def test_it_does_not_use_the_default_token(self, step):
        """
        A push made with GITHUB_TOKEN starts no workflow run. Updating a branch
        with it leaves the pull request up to date carrying the check runs of
        its previous head: green-looking and permanently unmergeable. The old
        queue's promotion step fell into exactly this.
        """
        token = step["with"]["github-token"]
        assert "PR_AUTOUPDATE_TOKEN" in token
        assert "GITHUB_TOKEN" not in token

    def test_a_missing_token_fails_loudly_and_changes_nothing(self, script):
        """
        Half of this operation is worse than none of it, so the absence of the
        secret is a red job, not a silent skip -- and the check comes before
        any call that could mutate a branch.
        """
        assert "core.setFailed" in script
        assert script.index("HAS_TOKEN") < script.index("updateBranch")

    def test_the_refusal_names_the_alternative(self, script):
        """
        Every gating workflow already declares `merge_group`, so the merge
        queue is a supported way out that needs no token at all. A refusal that
        does not say so is a dead end.
        """
        assert "merge queue" in script
        assert "merge_group" in script


class TestItUpdatesExactlyOne:
    def test_it_returns_after_the_first_successful_update(self, script):
        """
        Updating every branch at once starts N full runs against a main that is
        about to move again, and N-1 of them answer a stale question. The chain
        continues on its own: the updated PR merges, that push fires this
        workflow again.
        """
        body = script[script.index("updateBranch") :]
        assert "return;" in body
        assert body.index("return;") < body.index("No open pull request is behind")

    def test_the_next_one_means_the_one_waiting_longest(self, script):
        assert "sort: 'created'" in script
        assert "direction: 'asc'" in script

    def test_a_draft_is_never_touched(self, script):
        """A draft is a human saying the work is not ready."""
        assert "pr.draft" in script

    def test_only_a_stale_branch_is_updated(self, script):
        """
        `behind` is the only state a branch update fixes. `dirty` is a real
        conflict and needs a person; `blocked` and `unstable` mean the checks
        have something to say, not that the branch is stale.
        """
        assert "!== 'behind'" in script

    def test_an_uncomputed_mergeability_is_retried_not_assumed(self, script):
        """
        GitHub computes `mergeable_state` asynchronously, and a PR read right
        after a push usually reports `unknown`. Treating that as "up to date"
        would skip the pull request this workflow exists to serve.
        """
        assert "'unknown'" in script
        assert "attempt < 3" in script

    def test_a_race_with_another_merge_is_not_a_failure(self, script):
        """422 means it stopped being behind between the read and the write."""
        assert "e.status === 422" in script


class TestGateLaw:
    def test_it_correctly_has_no_gate_job(self, spec):
        """
        The gate law covers `pull_request` workflows, where a missing required
        check silently satisfies branch protection. This one runs on `push`.
        """
        assert "gate" not in spec["jobs"]
        triggers = spec.get("on") or spec.get(True)
        assert "pull_request" not in triggers

    def test_it_is_not_a_queue(self, script):
        """
        The pull request queue was removed because parking entries as drafts
        produced `skipped` required checks that branch protection counted as
        satisfied. Nothing here drafts, parks or closes anything.
        """
        for forbidden in ("toDraft", "markPullRequestReadyForReview", "update(", "close"):
            assert forbidden not in script
