"""
GOV-062 -- branch audits are SHA-qualified, recomputed and reconciled.

What these tests would catch:

* an audit that stays "done" after its branch moved (it must go STALE);
* a CI or PR attestation surviving a new commit it says nothing about;
* a branch that appeared after the audit, or vanished, not surfacing as a
  reconciliation failure -- i.e. a ledger that can be complete from memory;
* uncommitted work in a worktree classified as a clean branch;
* a branch whose history cannot be read locally reported clean instead of
  BLOCKED.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.agent_control.branch_audit import (
    BranchObservation,
    BranchRef,
    WorktreeAudit,
    classify_entry,
    collect_inventory,
    collect_refs,
    history_status,
    record_attestation,
    run_audit,
    verify_ledger,
    worktree_audits,
)
from src.agent_control.gitstate import GitResult
from src.agent_control.model import (
    SCHEMA_VERSION,
    BranchLedger,
    BranchState,
    ManifestError,
    StatusEntry,
)

NOW = "2026-10-07T00:00:00+00:00"
SHA_A = "a" * 40
SHA_B = "b" * 40
CLEAN = BranchState.AUDITED_CLEAN
UNCOMMITTED = BranchState.AUDITED_WITH_UNCOMMITTED_WORK
CONFLICT = BranchState.AUDITED_WITH_CONFLICT


def _ref(name: str, sha: str = SHA_A, *, remote: bool = False) -> BranchRef:
    ref = f"refs/remotes/{name}" if remote else f"refs/heads/{name}"
    return BranchRef(name, ref, sha, remote, None if remote else f"origin/{name}", True)


def _obs(name: str, sha: str = SHA_A, history: str = "MERGED", tree=None) -> BranchObservation:
    return BranchObservation(ref=_ref(name, sha), history=history, worktree=tree)


def _tree(*entries: StatusEntry, operations: tuple[str, ...] = ()) -> WorktreeAudit:
    return WorktreeAudit(path="/w", entries=entries, operations=operations)


def _audit(*observations: BranchObservation, ledger: BranchLedger | None = None) -> BranchLedger:
    base = ledger or BranchLedger(schema_version=SCHEMA_VERSION)
    return run_audit(base, list(observations), audit_id="BA-1", now=NOW)


class TestClassification:
    @pytest.mark.parametrize(
        ("history", "tree", "state"),
        [
            ("MERGED", None, CLEAN),
            ("AHEAD:3", None, BranchState.AUDITED_WITH_COMMITTED_WORK),
            ("UNAVAILABLE:commit not fetched", None, BranchState.BLOCKED),
            ("MERGED", _tree(), CLEAN),
            ("MERGED", _tree(StatusEntry("changed", ".M", "a")), UNCOMMITTED),
            ("MERGED", _tree(StatusEntry("changed", "A.", "a")), UNCOMMITTED),
            ("MERGED", _tree(StatusEntry("untracked", "??", "a")), UNCOMMITTED),
            ("MERGED", _tree(StatusEntry("unmerged", "UU", "a")), CONFLICT),
            ("MERGED", _tree(operations=("merge",)), CONFLICT),
            ("MERGED", _tree(operations=("cherry-pick",)), CONFLICT),
            ("MERGED", _tree(operations=("rebase",)), CONFLICT),
        ],
    )
    def test_state_from_history_and_worktree(self, history, tree, state) -> None:
        (entry,) = _audit(_obs("b", history=history, tree=tree)).entries
        assert entry.completion_status is state
        expected = "UNVERIFIED" if state is BranchState.BLOCKED else "VERIFIED"
        assert entry.verification_status == expected

    def test_worktree_fields_describe_what_was_found(self) -> None:
        tree = _tree(
            StatusEntry("changed", "MM", "both"),
            StatusEntry("renamed", "R.", "new", "old"),
            StatusEntry("untracked", "??", "u"),
        )
        (entry,) = _audit(_obs("b", tree=tree)).entries
        assert (entry.working_tree_status, entry.index_status) == ("DIRTY:1", "STAGED:2")
        assert entry.untracked_status == "UNTRACKED:1"
        assert (entry.merge_state, entry.rebase_state) == ("NONE", "NONE")

    def test_no_worktree_is_recorded_as_such(self) -> None:
        (entry,) = _audit(_obs("b")).entries
        assert entry.working_tree_status == "NO_WORKTREE"
        assert entry.remote_ref == "origin/b"

    def test_attested_ci_failure_outranks_an_open_pr(self) -> None:
        (entry,) = _audit(_obs("b")).entries
        state, finding = classify_entry(replace(entry, pr_state="open", related_pr=4))
        assert state is BranchState.AUDITED_WITH_OPEN_PR
        assert finding == "PR #4 open (attested)"
        failing = replace(entry, pr_state="open", ci_state="failure")
        assert classify_entry(failing)[0] is BranchState.AUDITED_WITH_CI_FAILURE


class TestAuditRuns:
    def test_attestations_survive_only_while_the_head_is_unchanged(self) -> None:
        ledger = _audit(_obs("b"))
        record_attestation(
            ledger, "b", SHA_A, pr=9, pr_state="open", ci_state="success", action="reviewed"
        )
        same = _audit(_obs("b"), ledger=ledger).get("b")
        assert same is not None
        assert (same.pr_state, same.action_taken, same.related_pr) == ("open", "reviewed", 9)
        moved = _audit(_obs("b", sha=SHA_B), ledger=ledger).get("b")
        assert moved is not None
        assert (moved.pr_state, moved.ci_state, moved.action_taken) == (None, None, "none")
        assert moved.related_pr == 9

    def test_a_vanished_branch_becomes_deleted_once(self) -> None:
        ledger = _audit(_obs("gone"), _obs("kept"))
        after = _audit(_obs("kept"), ledger=ledger)
        gone = after.get("gone")
        assert gone is not None and gone.completion_status is BranchState.DELETED
        again = _audit(_obs("kept"), ledger=replace(after, entries=list(after.entries)))
        assert again.get("gone") == gone
        assert [e.branch_name for e in again.entries] == ["gone", "kept"]


class TestAttestation:
    def test_unknown_or_moved_branches_are_refused(self) -> None:
        ledger = _audit(_obs("b"))
        with pytest.raises(ManifestError, match="no audit entry"):
            record_attestation(ledger, "x", SHA_A)
        with pytest.raises(ManifestError, match="moved since its audit"):
            record_attestation(ledger, "b", SHA_B)

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [({"pr_state": "draft"}, "pr_state"), ({"ci_state": "red"}, "ci_state")],
    )
    def test_unknown_states_are_refused(self, kwargs, message) -> None:
        with pytest.raises(ManifestError, match=message):
            record_attestation(_audit(_obs("b")), "b", SHA_A, **kwargs)

    def test_an_attestation_reclassifies_and_keeps_evidence(self) -> None:
        ledger = _audit(_obs("b"))
        entry = record_attestation(
            ledger, "b", SHA_A, ci_state="failure", issue=3, evidence="notice#1"
        )
        assert entry.completion_status is BranchState.AUDITED_WITH_CI_FAILURE
        assert entry.related_issue == 3
        assert entry.evidence[-1] == "notice#1"
        assert ledger.get("b") == entry
        unchanged = record_attestation(ledger, "b", SHA_A)
        assert unchanged.evidence == entry.evidence


class TestVerification:
    def test_a_fresh_audit_reconciles(self) -> None:
        inventory = [_obs("a"), _obs("b", history="AHEAD:1")]
        report = verify_ledger(_audit(*inventory), inventory, now=NOW)
        assert report["reconciled"]
        counts = ("inventory_count", "ledger_count", "classified_count")
        assert [report[key] for key in counts] == [2, 2, 2]
        assert report["by_state"] == {"AUDITED_CLEAN": 1, "AUDITED_WITH_COMMITTED_WORK": 1}

    def test_missing_extra_stale_and_changed_are_all_reported(self) -> None:
        ledger = _audit(_obs("moved"), _obs("removed"), _obs("dirtied"))
        inventory = [
            _obs("moved", sha=SHA_B),
            _obs("dirtied", tree=_tree(StatusEntry("untracked", "??", "x"))),
            _obs("new"),
        ]
        report = verify_ledger(ledger, inventory, now=NOW)
        assert not report["reconciled"]
        assert report["missing"] == ["new"]
        assert report["extra"] == ["removed"]
        assert report["stale"] == ["moved"]
        assert report["changed"] == ["dirtied"]
        moved = ledger.get("moved")
        assert moved is not None
        assert (moved.completion_status, moved.stale_since_sha) == (BranchState.STALE, SHA_B)
        again = verify_ledger(ledger, inventory, now=NOW)
        assert again["stale"] == ["moved"]
        assert ledger.get("moved") == moved


class TestInventoryFromGit:
    def test_refs_skip_symbolic_heads_and_trust_the_remote(self, fake_git, tmp_path: Path) -> None:
        refs = "\n".join(
            [
                f"refs/heads/main\0{SHA_A}\0origin/main",
                f"refs/heads/local\0{SHA_A}\0",
                f"refs/remotes/origin/HEAD\0{SHA_A}\0",
                f"refs/remotes/origin/main\0{SHA_A}\0",
            ]
        )
        git = fake_git(
            {
                (
                    "for-each-ref",
                    "--format=%(refname)%00%(objectname)%00%(upstream:short)",
                    "refs/heads",
                    "refs/remotes/origin",
                ): refs,
                ("ls-remote", "--heads", "origin"): (
                    f"{SHA_B}\trefs/heads/main\n\n{SHA_A}\trefs/heads/new\n"
                ),
                ("cat-file", "--batch-check"): f"{SHA_B} missing\n{SHA_A} commit 200\n",
            },
            tmp_path,
        )
        found = {r.name: r for r in collect_refs(git, remote="origin", include_remote_heads=True)}
        assert sorted(found) == ["local", "main", "origin/main", "origin/new"]
        assert found["local"].upstream is None and found["main"].upstream == "origin/main"
        assert (found["origin/main"].sha, found["origin/main"].objects_present) == (SHA_B, False)
        assert found["origin/new"].objects_present

    def test_an_empty_remote_needs_no_object_lookup(self, fake_git, tmp_path: Path) -> None:
        listing = ("for-each-ref", "--format=%(refname)%00%(objectname)%00%(upstream:short)")
        git = fake_git(
            {
                (*listing, "refs/heads", "refs/remotes/origin"): "",
                ("ls-remote", "--heads", "origin"): "",
            },
            tmp_path,
        )
        assert collect_refs(git, remote="origin", include_remote_heads=True) == []

    def test_history_status_names_why_it_cannot_tell(self, fake_git, tmp_path: Path) -> None:
        ref = _ref("b")
        git = fake_git(
            {
                ("merge-base", SHA_B, SHA_A): GitResult((), 1, "", ""),
            },
            tmp_path,
        )
        assert history_status(git, None, ref) == "UNAVAILABLE:base ref not found"
        unfetched = replace(ref, objects_present=False)
        assert history_status(git, SHA_B, unfetched) == "UNAVAILABLE:commit not fetched"
        assert history_status(git, SHA_B, ref).startswith("UNAVAILABLE:no common history")

    def test_inventory_of_a_real_repository(self, repo, tmp_path: Path) -> None:
        repo.commit("f.txt", "f\n")
        side = tmp_path / "side"
        repo.git.run("worktree", "add", "-q", "-b", "side", str(side))
        (side / "wip.txt").write_text("wip\n", encoding="utf-8")
        repo.git.run("worktree", "add", "-q", "--detach", str(tmp_path / "detached"))
        observed = collect_inventory(
            repo.git, base="origin/main", remote="origin", include_remote_heads=False
        )
        inventory = {obs.ref.name: obs for obs in observed}
        assert sorted(inventory) == ["feature", "main", "origin/main", "side"]
        assert inventory["feature"].history == "AHEAD:1"
        assert inventory["main"].history == "MERGED"
        assert inventory["origin/main"].worktree is None
        side_tree = inventory["side"].worktree
        assert side_tree is not None and side_tree.untracked == 1
        trees = worktree_audits(repo.git)
        assert set(trees) == {"feature", "side"}
        missing_base = collect_inventory(
            repo.git, base="nope", remote="origin", include_remote_heads=False
        )
        assert {obs.history for obs in missing_base} == {"UNAVAILABLE:base ref not found"}
