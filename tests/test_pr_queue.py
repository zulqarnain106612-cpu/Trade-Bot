"""
The serial queue's invariants, pinned where they can be checked.

The queue exists because GitHub's own merge queue has the wrong failure
behaviour: it dequeues a failing entry and starts the next one, which sets the
failure aside instead of finishing it. This one keeps exactly one pull request
active, keeps it active while it is red, and starts nothing else until it
merges.

Two properties carry that guarantee, and both are cheap to break by accident:
a parked entry must not run CI (or the queue costs as much as no queue), and
promotion must update the branch before revealing it (or the promoted entry
never gets a run at all). Both are asserted here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"
QUEUE = WORKFLOWS / "pr-queue.yml"

ADVISORY = {"claude-review.yml"}
NOT_A_GATE = {"ci-failure-notify.yml", "pr-queue.yml"}

DRAFT_GUARD = "github.event.pull_request.draft"


def _triggers(spec: dict) -> dict:
    return spec.get("on") or spec.get(True) or {}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def gating_workflows() -> list[Path]:
    out = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        if path.name in ADVISORY or path.name in NOT_A_GATE:
            continue
        if "pull_request" in _triggers(_load(path)):
            out.append(path)
    return out


def _script() -> str:
    steps = _load(QUEUE)["jobs"]["reconcile"]["steps"]
    return "\n".join(s.get("with", {}).get("script", "") for s in steps)


class TestParkedEntriesAreFree:
    """A parked entry must cost nothing, or the queue is pure overhead."""

    def test_there_are_gating_workflows_to_check(self):
        assert gating_workflows()

    @pytest.mark.parametrize("path", gating_workflows(), ids=lambda p: p.name)
    def test_every_job_skips_on_a_draft(self, path):
        spec = _load(path)
        unguarded = [
            name for name, job in spec["jobs"].items() if DRAFT_GUARD not in str(job.get("if", ""))
        ]
        assert not unguarded, (
            f"{path.name}: {unguarded} would run on a parked (draft) pull "
            "request and spend runner time on an entry that cannot merge."
        )

    @pytest.mark.parametrize("path", gating_workflows(), ids=lambda p: p.name)
    def test_the_gate_still_runs_when_a_dependency_fails(self, path):
        """
        The draft guard must not cost the gate its `always()`.

        CLAUDE.md is explicit: without `always()` the gate is skipped the
        moment a dependency fails, and a skipped required check blocks
        nothing. The guard is ANDed onto it, never a replacement for it.
        """
        gate = _load(path)["jobs"].get("gate")
        if gate is None:
            pytest.skip(f"{path.name} has no gate job")
        condition = str(gate["if"])
        assert "always()" in condition
        assert DRAFT_GUARD in condition


class TestPromotionOrder:
    def test_the_branch_is_updated_before_the_entry_is_revealed(self):
        """
        Update, then reveal -- in that order, for a mechanical reason.

        A push made with GITHUB_TOKEN does not trigger workflows, so updating
        a branch produces no run. `ready_for_review` does trigger one. Reveal
        first and the entry sits at the front of the queue with no run and no
        way to get one: a silent stall at the worst possible position.
        """
        script = _script()
        assert "updateBranch" in script
        assert "markPullRequestReadyForReview" in script
        assert script.index("updateBranch") < script.index("markPullRequestReadyForReview")

    def test_a_failed_update_leaves_the_entry_parked_and_says_so(self):
        """A conflict must be reported, not promoted into."""
        script = _script()
        assert "createComment" in script
        assert "cannot update this branch" in script

    def test_auto_merge_is_armed_on_promotion(self):
        """
        Auto-merge cannot be set on a draft, so it is armed at the moment the
        entry goes active rather than when it was opened.
        """
        assert "enablePullRequestAutoMerge" in _script()

    def test_it_squashes(self):
        assert "SQUASH" in _script()


class TestSerialGuarantee:
    def test_only_one_entry_is_ever_active(self):
        """Everything after the oldest active entry is parked."""
        script = _script()
        assert "active.slice(1)" in script
        assert "convertPullRequestToDraft" in script

    def test_a_red_entry_holds_the_line(self):
        """
        Nothing is promoted while an active entry exists, whatever its checks
        say. This is the precise behaviour GitHub's merge queue does not have:
        a failing entry is finished, not set aside.
        """
        script = _script()
        front = script.index("const front = active[0]")
        promote = script.index("markPullRequestReadyForReview")
        between = script[front:promote]
        assert "return" in between, "promotion is not short-circuited while an entry is active"

    def test_entries_are_served_oldest_first(self):
        assert "a.number - b.number" in _script()

    def test_a_hand_made_draft_is_left_alone(self):
        """
        Only labelled drafts are queue entries. A draft someone made by hand
        is work in progress, and promoting it would publish unfinished work.
        """
        script = _script()
        assert "labelled(pr)" in script

    def test_the_controller_cannot_race_itself(self):
        """Two copies could promote two entries and break the invariant."""
        queue = _load(QUEUE)
        assert queue["concurrency"]["group"] == "pr-queue"
        assert queue["concurrency"]["cancel-in-progress"] is False


class TestItCannotStallSilently:
    def test_a_missed_event_is_recovered_by_a_schedule(self):
        """
        Events do get missed, and a queue that stalls quietly is the failure
        this whole mechanism exists to remove. The schedule is the backstop.
        """
        triggers = _triggers(_load(QUEUE))
        assert "schedule" in triggers
        assert "workflow_dispatch" in triggers

    def test_it_promotes_on_a_merge(self):
        triggers = _triggers(_load(QUEUE))
        assert triggers["push"]["branches"] == ["main"]

    def test_it_parks_new_entries_as_they_arrive(self):
        triggers = _triggers(_load(QUEUE))
        assert "opened" in triggers["pull_request_target"]["types"]


class TestPermissions:
    def test_it_has_exactly_what_it_needs(self):
        perms = _load(QUEUE)["permissions"]
        # contents: write is required by updateBranch, which pushes a merge
        # commit onto the entry's branch.
        assert perms["contents"] == "write"
        assert perms["pull-requests"] == "write"

    def test_it_never_checks_out_pull_request_code(self):
        """
        `pull_request_target` runs with a write token in the base repository's
        context. Checking out the entry's code under that token is the classic
        way to hand a fork write access, so this workflow does not do it.
        """
        spec = _load(QUEUE)
        for job in spec["jobs"].values():
            for step in job.get("steps", []):
                if "actions/checkout" not in str(step.get("uses", "")):
                    continue
                # A bare checkout under `pull_request_target` takes the base
                # repository, which is safe. Naming a ref is how the entry's
                # own code gets fetched, and that is what must not happen.
                with_ = step.get("with") or {}
                assert "ref" not in with_ and "repository" not in with_, (
                    "pr-queue checks out pull request code under a write token"
                )


class TestZeroCheckStall:
    """
    The stall that notifies nobody, because nothing failed.

    Retargeting a pull request's base fires none of opened, synchronize or
    reopened, so no workflow triggers. The entry sits at the front of the
    queue with zero check runs: blocked by branch protection, invisible to a
    failure notice, and waiting on an event that is never coming. It happened
    on #298. The controller now detects it and reopens the entry, which does
    fire an event.
    """

    def test_the_front_entry_is_checked_for_having_no_runs(self):
        script = _script()
        assert "listWorkflowRunsForRepo" in script
        assert "ours.length === 0" in script

    def test_it_reopens_rather_than_pushing_a_commit(self):
        """
        Reopening fires `reopened`; an empty commit would also work but
        rewrites the branch for a problem that is not in the code.
        """
        script = _script()
        assert "state: 'closed'" in script
        assert "state: 'open'" in script

    def test_it_only_counts_pull_request_runs(self):
        """
        GitHub attaches its own dynamic runs (code scanning autofix) to a
        pull request. Counting those would hide the stall, because the entry
        would look like it had checks when none of ours had started.
        """
        assert "r.event === 'pull_request'" in _script()

    def test_it_says_what_it_did(self):
        assert "no checks had started" in _script()


class TestTheFrontEntryIsKeptCurrent:
    """
    The front entry has already been promoted, so nothing updates it again.

    The ruleset requires an up-to-date branch. Promotion is what normally
    satisfies that, and promotion happens once. An entry that is active and
    behind -- because it was active before the queue existed, or because main
    moved under it -- would sit at the front blocked forever, with green
    checks and nothing failed. Observed on #248 the moment the queue went
    live.
    """

    def test_a_stale_front_entry_is_brought_forward(self):
        script = _script()
        assert "mergeable_state === 'behind'" in script

    def test_it_updates_the_front_entry_before_reporting_it_holds_the_line(self):
        script = _script()
        assert script.index("mergeable_state === 'behind'") < script.index(
            "holds the line; nothing is promoted"
        )

    def test_a_failed_update_does_not_stop_the_pass(self):
        """
        Promotion aborts on a failed update because promoting into a conflict
        wedges the queue. Here the entry is already active, so the pass simply
        warns and moves on: there is nothing to abort.
        """
        script = _script()
        tail = script[script.index("mergeable_state === 'behind'") :]
        head = tail[: tail.index("holds the line; nothing is promoted")]
        assert "core.warning" in head


class TestTheQueueAdvancesWithoutAnEvent:
    """
    The queue cannot depend on being told that an entry merged.

    A push made with GITHUB_TOKEN does not trigger workflows. The controller
    arms auto-merge with that token, so the merge it produces raises no push
    event and never wakes this workflow. It was observed directly: #248 was
    armed by a human token and its merge triggered a run that promoted #258;
    #258 was armed by the controller, and its merge triggered nothing, leaving
    the queue idle until the schedule fired.

    So a single pass both finishes the front entry and starts the next one.
    """

    def test_a_clean_front_entry_is_merged_in_the_same_pass(self):
        script = _script()
        assert "mergeable_state === 'clean'" in script
        assert "pulls.merge" in script

    def test_merging_clears_the_active_slot_so_the_pass_continues(self):
        """
        The promotion below is guarded on there being no active entry, so the
        merge has to empty that slot or the pass would stop one line later
        and the next entry would wait for an event that is not coming.
        """
        script = _script()
        merge_at = script.index("pulls.merge")
        assert "active = []" in script[merge_at : merge_at + 600]
        assert "if (active.length > 0)" in script

    def test_a_failed_merge_leaves_the_entry_holding_the_line(self):
        """Not mergeable yet is the normal case, not an error."""
        script = _script()
        merge_at = script.index("pulls.merge")
        assert "core.warning" in script[merge_at : merge_at + 600]
        assert "holds the line; nothing is promoted" in script

    def test_it_squashes_here_too(self):
        script = _script()
        assert "merge_method: 'squash'" in script

    def test_the_heartbeat_is_frequent_enough_to_be_the_only_trigger(self):
        """
        With events unreliable, the schedule is not a backstop any more --
        it is the mechanism. Thirty minutes would mean the queue advances
        twice an hour.
        """
        cron = _triggers(_load(QUEUE))["schedule"][0]["cron"]
        minutes = cron.split()[0]
        assert minutes.startswith("*/")
        assert int(minutes[2:]) <= 5
