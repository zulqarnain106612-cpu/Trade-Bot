"""
Branch and worktree audit -- SHA-qualified, recomputed, reconciled.

An audit is a claim about a branch *at a commit*. Every ledger entry stores
the SHA it audited; when the branch moves, the entry becomes STALE instead of
silently staying "done". ``verify_ledger`` recomputes the inventory from Git
rather than trusting the ledger, and reports reconciled only when

    inventory count == ledger count == classified count

with nothing missing, extra, stale or reclassified -- so a branch counted from
memory, a branch "handled" because a similarly named one was, or a branch
created after the audit all surface as a reconciliation failure.

Uncommitted work belongs to a worktree, not to a branch ref, so
classification reads the status of every worktree: a branch checked out and
dirty in another worktree is AUDITED_WITH_UNCOMMITTED_WORK, not clean.

PR and CI facts cannot be read from Git. They are recorded as attestations
against the audited SHA and are dropped the moment the branch moves, because
a CI result for an older commit says nothing about the current one.

Registry: GOV-062 (config/quality_registry.json).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from typing import Any

from src.agent_control.gitstate import (
    GitLike,
    active_operations,
    parse_status_v2,
    parse_worktrees,
)
from src.agent_control.model import (
    BranchAuditEntry,
    BranchLedger,
    BranchState,
    ManifestError,
    StatusEntry,
)

NO_WORKTREE = "NO_WORKTREE"
PR_STATES = frozenset({"open", "closed", "merged"})
CI_STATES = frozenset({"success", "failure", "pending"})


@dataclass(frozen=True, slots=True)
class BranchRef:
    name: str
    ref: str
    sha: str
    remote: bool
    upstream: str | None
    objects_present: bool


@dataclass(frozen=True, slots=True)
class WorktreeAudit:
    path: str
    entries: tuple[StatusEntry, ...]
    operations: tuple[str, ...]

    @property
    def unmerged(self) -> int:
        return sum(1 for e in self.entries if e.kind == "unmerged")

    @property
    def staged(self) -> int:
        return sum(1 for e in self.entries if e.kind in ("changed", "renamed") and e.xy[0] != ".")

    @property
    def unstaged(self) -> int:
        return sum(1 for e in self.entries if e.kind in ("changed", "renamed") and e.xy[1] != ".")

    @property
    def untracked(self) -> int:
        return sum(1 for e in self.entries if e.kind == "untracked")


@dataclass(frozen=True, slots=True)
class BranchObservation:
    ref: BranchRef
    history: str
    worktree: WorktreeAudit | None


def collect_refs(runner: GitLike, *, remote: str, include_remote_heads: bool) -> list[BranchRef]:
    """Local branches, remote-tracking branches and (optionally) the remote's own heads."""
    out = runner.run(
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(upstream:short)",
        "refs/heads",
        f"refs/remotes/{remote}",
    ).stdout
    refs: dict[str, BranchRef] = {}
    for line in out.splitlines():
        ref, sha, upstream = line.split("\0")
        if ref.endswith("/HEAD"):
            continue
        is_remote = ref.startswith("refs/remotes/")
        name = ref.removeprefix("refs/remotes/" if is_remote else "refs/heads/")
        refs[name] = BranchRef(name, ref, sha, is_remote, upstream or None, True)
    if include_remote_heads:
        heads = runner.run("ls-remote", "--heads", remote).stdout.splitlines()
        listed = [line.split("\t") for line in heads if line.strip()]
        present = _present_objects(runner, [sha for sha, _ in listed])
        for sha, ref in listed:
            name = f"{remote}/{ref.removeprefix('refs/heads/')}"
            # The remote is the authority on its own heads: a local tracking
            # ref that disagrees is simply out of date.
            tracking = f"refs/remotes/{name}"
            refs[name] = BranchRef(name, tracking, sha, True, None, sha in present)
    return [refs[name] for name in sorted(refs)]


def _present_objects(runner: GitLike, shas: list[str]) -> set[str]:
    if not shas:
        return set()
    out = runner.run("cat-file", "--batch-check", input_text="\n".join(shas) + "\n").stdout
    present: set[str] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "commit":
            present.add(parts[0])
    return present


def history_status(runner: GitLike, base_sha: str | None, ref: BranchRef) -> str:
    """``MERGED``, ``AHEAD:<n>``, or ``UNAVAILABLE:<why>`` when Git cannot say."""
    if base_sha is None:
        return "UNAVAILABLE:base ref not found"
    if not ref.objects_present:
        return "UNAVAILABLE:commit not fetched"
    if runner.run("merge-base", base_sha, ref.sha, check=False).returncode != 0:
        return "UNAVAILABLE:no common history locally (shallow clone?)"
    ahead = int(runner.run("rev-list", "--count", f"{base_sha}..{ref.sha}").stdout.strip())
    return f"AHEAD:{ahead}" if ahead else "MERGED"


def worktree_audits(runner: GitLike) -> dict[str, WorktreeAudit]:
    """Branch name -> status of the worktree that has it checked out."""
    audits: dict[str, WorktreeAudit] = {}
    for tree in parse_worktrees(runner.run("worktree", "list", "--porcelain").stdout):
        if tree.branch is None or tree.bare or tree.prunable:
            continue
        status = runner.run(
            "status", "--porcelain=v2", "-z", "--untracked-files=all", cwd=tree.path
        )
        audits[tree.branch] = WorktreeAudit(
            path=tree.path,
            entries=tuple(parse_status_v2(status.stdout)[1]),
            operations=tuple(active_operations(runner, cwd=tree.path)),
        )
    return audits


def collect_inventory(
    runner: GitLike, *, base: str, remote: str, include_remote_heads: bool
) -> list[BranchObservation]:
    refs = collect_refs(runner, remote=remote, include_remote_heads=include_remote_heads)
    base_result = runner.run("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False)
    base_sha = base_result.stdout.strip() if base_result.returncode == 0 else None
    worktrees = worktree_audits(runner)
    return [
        BranchObservation(
            ref=ref,
            history=history_status(runner, base_sha, ref),
            worktree=None if ref.remote else worktrees.get(ref.name),
        )
        for ref in refs
    ]


def _worktree_fields(tree: WorktreeAudit | None) -> dict[str, str]:
    if tree is None:
        return {
            "working_tree_status": NO_WORKTREE,
            "untracked_status": NO_WORKTREE,
            "index_status": NO_WORKTREE,
            "merge_state": NO_WORKTREE,
            "rebase_state": NO_WORKTREE,
        }
    if "merge" in tree.operations:
        merge = "MERGE_IN_PROGRESS"
    elif "cherry-pick" in tree.operations:
        merge = "CHERRY_PICK_IN_PROGRESS"
    elif tree.unmerged:
        merge = f"UNMERGED:{tree.unmerged}"
    else:
        merge = "NONE"
    return {
        "working_tree_status": f"DIRTY:{tree.unstaged}" if tree.unstaged else "CLEAN",
        "untracked_status": f"UNTRACKED:{tree.untracked}" if tree.untracked else "NONE",
        "index_status": f"STAGED:{tree.staged}" if tree.staged else "CLEAN",
        "merge_state": merge,
        "rebase_state": "REBASE_IN_PROGRESS" if "rebase" in tree.operations else "NONE",
    }


def classify_entry(entry: BranchAuditEntry) -> tuple[BranchState, str]:
    """The branch state the recorded facts imply, and the finding that says why."""
    if {entry.merge_state, entry.rebase_state} - {"NONE", NO_WORKTREE}:
        finding = f"worktree mid-operation: merge={entry.merge_state} rebase={entry.rebase_state}"
        return BranchState.AUDITED_WITH_CONFLICT, finding
    dirty = [
        status
        for status in (entry.working_tree_status, entry.untracked_status, entry.index_status)
        if status not in ("CLEAN", "NONE", NO_WORKTREE)
    ]
    if dirty:
        return BranchState.AUDITED_WITH_UNCOMMITTED_WORK, "uncommitted work: " + ", ".join(dirty)
    if entry.ci_state == "failure":
        return BranchState.AUDITED_WITH_CI_FAILURE, "CI failing at the audited SHA (attested)"
    if entry.pr_state == "open":
        return BranchState.AUDITED_WITH_OPEN_PR, f"PR #{entry.related_pr} open (attested)"
    if entry.commit_history_status.startswith("UNAVAILABLE"):
        return BranchState.BLOCKED, entry.commit_history_status
    if entry.commit_history_status.startswith("AHEAD"):
        count = entry.commit_history_status.split(":", 1)[1]
        return BranchState.AUDITED_WITH_COMMITTED_WORK, f"{count} commit(s) not in base"
    return BranchState.AUDITED_CLEAN, "fully contained in base; no worktree changes"


def _classified(entry: BranchAuditEntry) -> BranchAuditEntry:
    state, finding = classify_entry(entry)
    verified = "UNVERIFIED" if state is BranchState.BLOCKED else "VERIFIED"
    return replace(entry, completion_status=state, finding=finding, verification_status=verified)


def _entry_for(
    obs: BranchObservation, previous: BranchAuditEntry | None, *, audit_id: str, now: str
) -> BranchAuditEntry:
    same_head = previous is not None and previous.audited_head_sha == obs.ref.sha
    keep = previous if same_head else None
    entry = BranchAuditEntry(
        branch_name=obs.ref.name,
        remote_ref=obs.ref.ref if obs.ref.remote else obs.ref.upstream,
        audit_id=audit_id,
        audited_head_sha=obs.ref.sha,
        audit_started_at=now,
        audit_completed_at=now,
        commit_history_status=obs.history,
        related_pr=previous.related_pr if previous else None,
        related_issue=previous.related_issue if previous else None,
        finding="",
        action_taken=keep.action_taken if keep else "none",
        verification_status="UNVERIFIED",
        completion_status=BranchState.BLOCKED,
        evidence=[f"{obs.ref.ref}@{obs.ref.sha}"],
        pr_state=keep.pr_state if keep else None,
        ci_state=keep.ci_state if keep else None,
        **_worktree_fields(obs.worktree),
    )
    return _classified(entry)


def run_audit(
    ledger: BranchLedger, inventory: list[BranchObservation], *, audit_id: str, now: str
) -> BranchLedger:
    """A new ledger auditing every inventoried branch; vanished branches become DELETED."""
    names = {obs.ref.name for obs in inventory}
    entries = [
        _entry_for(obs, ledger.get(obs.ref.name), audit_id=audit_id, now=now) for obs in inventory
    ]
    for old in ledger.entries:
        if old.branch_name in names:
            continue
        if old.completion_status is not BranchState.DELETED:
            old = replace(
                old,
                audit_id=audit_id,
                audit_completed_at=now,
                completion_status=BranchState.DELETED,
                finding="ref no longer exists",
            )
        entries.append(old)
    return BranchLedger(
        schema_version=ledger.schema_version,
        entries=sorted(entries, key=lambda e: e.branch_name),
    )


def record_attestation(
    ledger: BranchLedger,
    branch: str,
    current_sha: str,
    *,
    pr: int | None = None,
    pr_state: str | None = None,
    ci_state: str | None = None,
    issue: int | None = None,
    action: str | None = None,
    evidence: str | None = None,
) -> BranchAuditEntry:
    """Attach facts Git cannot see (PR, CI, issue, action) to a fresh audit entry."""
    entry = ledger.get(branch)
    if entry is None:
        raise ManifestError(f"{branch!r} has no audit entry; run the audit first")
    if entry.audited_head_sha != current_sha:
        raise ManifestError(f"{branch!r} moved since its audit; re-run the audit first")
    if pr_state is not None and pr_state not in PR_STATES:
        raise ManifestError(f"pr_state must be one of {sorted(PR_STATES)}")
    if ci_state is not None and ci_state not in CI_STATES:
        raise ManifestError(f"ci_state must be one of {sorted(CI_STATES)}")
    updated = replace(
        entry,
        related_pr=pr if pr is not None else entry.related_pr,
        related_issue=issue if issue is not None else entry.related_issue,
        pr_state=pr_state if pr_state is not None else entry.pr_state,
        ci_state=ci_state if ci_state is not None else entry.ci_state,
        action_taken=action if action is not None else entry.action_taken,
        evidence=[*entry.evidence, evidence] if evidence else list(entry.evidence),
    )
    updated = _classified(updated)
    ledger.entries[ledger.entries.index(entry)] = updated
    return updated


def verify_ledger(
    ledger: BranchLedger, inventory: list[BranchObservation], *, now: str
) -> dict[str, Any]:
    """
    Recompute and compare. Entries whose branch moved are marked STALE in
    ``ledger`` (the caller persists it); the report is reconciled only when
    every count agrees and nothing is missing, extra, stale or reclassified.
    """
    current = {obs.ref.name: obs for obs in inventory}
    live = {
        e.branch_name: e for e in ledger.entries if e.completion_status is not BranchState.DELETED
    }
    missing = sorted(set(current) - set(live))
    extra = sorted(set(live) - set(current))
    stale: list[str] = []
    changed: list[str] = []
    for name in sorted(set(current) & set(live)):
        entry, obs = live[name], current[name]
        if entry.audited_head_sha != obs.ref.sha or entry.completion_status is BranchState.STALE:
            stale.append(name)
            if entry.completion_status is not BranchState.STALE:
                marked = replace(
                    entry,
                    completion_status=BranchState.STALE,
                    stale_since_sha=obs.ref.sha,
                    finding=f"branch moved to {obs.ref.sha[:12]} after the audit",
                    audit_completed_at=now,
                )
                ledger.entries[ledger.entries.index(entry)] = marked
            continue
        fresh = _entry_for(obs, entry, audit_id=entry.audit_id, now=now)
        if fresh.completion_status is not entry.completion_status:
            changed.append(name)
    classified = len(set(current) & set(live)) - len(stale) - len(changed)
    reconciled = (
        not (missing or extra or stale or changed) and len(current) == len(live) == classified
    )
    return {
        "reconciled": reconciled,
        "inventory_count": len(current),
        "ledger_count": len(live),
        "classified_count": classified,
        "missing": missing,
        "extra": extra,
        "stale": stale,
        "changed": changed,
        "by_state": dict(sorted(Counter(str(e.completion_status) for e in ledger.entries).items())),
    }
