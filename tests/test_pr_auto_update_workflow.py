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
        `behind` is the only state a branch *update* fixes, because
        updateBranch is an API call and a conflict is not. `blocked` and
        `unstable` mean the checks have something to say, not that the branch
        is stale; `dirty` is handed to `resolve-next` instead.
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


@pytest.fixture(scope="module")
def resolve(spec: dict) -> dict:
    return spec["jobs"]["resolve-next"]


@pytest.fixture(scope="module")
def resolve_script(resolve: dict) -> str:
    (only,) = [s for s in resolve["steps"] if "run" in s]
    return only["run"]


class TestHandover:
    """
    `update-next` nominates the conflicted pull request; it does not resolve
    one. The nomination has to be worth acting on and has to cost nothing when
    an update already happened.
    """

    def test_a_fork_is_never_nominated(self, script):
        """
        Resolving means pushing to the head branch, and this workflow's token
        has no write access to a fork. Nominating one would hand `resolve-next`
        a pull request it can only fail on.
        """
        assert "data.head.repo.full_name === `${context.repo.owner}/${context.repo.repo}`" in script

    def test_nothing_is_nominated_when_a_branch_was_updated(self, script):
        """
        One pull request per merge, as before. The update path returns before
        the nomination is published, so a run that updated a branch hands over
        an empty string and `resolve-next` is skipped.
        """
        body = script[script.index("updateBranch") :]
        assert body.index("return;") < body.index("core.setOutput('resolve_pr'")

    def test_the_oldest_conflicted_one_is_nominated(self, script):
        """First come, first served -- the same order the update path uses."""
        assert "!dirtyNumber" in script


class TestResolveNext:
    def test_it_runs_only_on_a_nomination(self, resolve):
        """
        Without the guard the job would run on every push to main, check out
        whatever `resolve_ref` happened to be, and merge main into it.
        """
        assert resolve["needs"] == "update-next"
        assert resolve["if"] == "needs.update-next.outputs.resolve_pr != ''"

    def test_it_uses_the_token_whose_pushes_start_runs(self, resolve):
        """
        Same trap as the update path: a push made with GITHUB_TOKEN starts no
        workflow run, so the pull request would end up resolved, green against
        its previous head, and unmergeable.
        """
        (checkout,) = [s for s in resolve["steps"] if "actions/checkout" in s.get("uses", "")]
        assert checkout["with"]["token"] == "${{ secrets.PR_AUTOUPDATE_TOKEN }}"

    def test_it_checks_out_the_whole_history(self, resolve):
        """A merge needs the merge base, and a shallow checkout has not got it."""
        (checkout,) = [s for s in resolve["steps"] if "actions/checkout" in s.get("uses", "")]
        assert checkout["with"]["fetch-depth"] == 0

    def test_it_installs_nothing(self, resolve):
        """
        Both generators and the gate are stdlib-only. A job that does not
        import the project does not install it (GOV-015), and an install here
        would be attributable to nothing.
        """
        run = " ".join(s.get("run", "") for s in resolve["steps"])
        assert "pip install" not in run
        assert not any("setup-uv" in s.get("uses", "") for s in resolve["steps"])

    @pytest.mark.parametrize(
        "path",
        ["docs/quality/REQUIREMENTS_TRACEABILITY.md", "docs/MATH_FOUNDATIONS.md"],
    )
    def test_only_generated_paths_are_resolvable(self, resolve_script, path):
        """
        The whole justification for resolving automatically is that a script
        produces the answer, so no judgement is exercised. A path with no
        generator has no such answer.
        """
        assert path in resolve_script

    def test_one_unknown_path_abandons_the_whole_merge(self, resolve_script):
        """
        Resolving the generated half of a conflict and leaving the rest would
        produce a branch that is neither merged nor clean, and would hide the
        real conflict behind a commit that looks like progress.
        """
        guard = resolve_script[resolve_script.index("conflicted paths") :]
        assert guard.index("git merge --abort") < guard.index("generate_quality_docs.py")

    def test_it_regenerates_rather_than_picking_a_side(self, resolve_script):
        """
        The correct content of a generated file is whatever its generator says
        from the merged inputs -- never `--ours`, never `--theirs`, either of
        which silently drops the other branch's entries.
        """
        assert "--ours" not in resolve_script
        assert "--theirs" not in resolve_script
        assert "python3 scripts/generate_quality_docs.py" in resolve_script
        assert "python3 scripts/generate_math_docs.py" in resolve_script

    def test_the_gate_runs_before_the_push(self, resolve_script):
        """
        The registry JSON merges *cleanly* and can still be wrong -- two
        branches allocating the same id is the usual way, since the scaffolder
        allocates against main and cannot see open branches. The gate loads it
        through the strict loader, which is what catches that; a push before
        the gate would put it on the head branch and eventually on main.
        """
        assert resolve_script.rindex("git push origin") > resolve_script.index("qe_gate.py")

    def test_a_clean_merge_is_gated_too(self, resolve_script):
        """
        A conflict is not the only way this merge goes wrong. The registry JSON
        merges cleanly and can still be refused by its loader, so the path that
        never saw a conflict must reach the same gate -- there is exactly one
        `git push` to the head branch, and it is after the gate.
        """
        assert resolve_script.count("git push origin") == 1

    def test_a_refused_gate_pushes_nothing(self, resolve_script):
        """
        Failing loudly is the correct half of this operation to perform. A
        broken registry reaching the head branch would be resolved, green in
        appearance, and refused by the loader on main.
        """
        assert "Nothing pushed." in resolve_script
        assert "exit 1" in resolve_script
