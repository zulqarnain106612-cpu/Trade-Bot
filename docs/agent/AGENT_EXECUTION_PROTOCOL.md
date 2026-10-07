# Agent execution protocol

How a Claude Code session records what it is doing so the repository -- not
the conversation -- decides whether the work is done. Implementation:
`src/agent_control/` (GOV-060 .. GOV-065); command line:
`scripts/agent_control.py`; hooks: `.claude/hooks/completion_gate.py`,
`precompact_checkpoint.py`, `task_lifecycle.py` and the guarded-Git check in
`pre_tool_use.py`.

## When to use it

Use a task for any substantial piece of work: more than one commit, more than
one session, or anything that ends in a push. Read-only questions and one-line
fixes do not need one -- with no active task every hook is a no-op.

## The record

A task manifest lives in the common Git directory
(`.git/agent-control/tasks/<task-id>.json`, or under `TB_AGENT_STATE_DIR`).
It is outside every work tree, so recording state never makes `git status`
dirty, and it is shared by every worktree of the clone, so work in another
worktree is never hidden.

Two independent axes:

| Axis | Values | Meaning |
|---|---|---|
| phase | CREATED, DISCOVERING, PLANNING, IMPLEMENTING, VERIFYING, AUDITING, READY_TO_COMMIT, COMMITTED, READY_TO_DELIVER, DELIVERED | where in the pipeline the work is; advisory, moves along validated edges |
| status | ACTIVE, COMPLETE, PAUSED, PAUSED_WITH_UNCOMMITTED_WIP, BLOCKED, FAILED, STALE, ABANDONED | whether the work is moving, and if not, why |

Status meanings:

* **ACTIVE** -- in progress. The Stop hook will not let a turn end while an
  ACTIVE task fails the completion validator.
* **PAUSED** -- stopped on a clean tree, with a reason and the next action.
* **PAUSED_WITH_UNCOMMITTED_WIP** -- stopped on a dirty tree. Every dirty path
  is recorded. A dirty tree can never be paused as plain PAUSED.
* **BLOCKED** -- cannot proceed without something external (a decision,
  access, a failing dependency). Reason and next action are required.
* **FAILED** -- the attempt did not work; resumable or abandonable.
* **STALE** -- HEAD moved after COMPLETE, so the completion proof no longer
  describes the code. Set automatically.
* **ABANDONED** -- terminal.
* **COMPLETE** -- reachable only through the completion validator, with a
  proof bound to the current HEAD.

Each deliverable separately records **REQUESTED -> IMPLEMENTED -> VERIFIED ->
COMMITTED -> PUSHED -> DELIVERED**.

## Workflow

Create the task before editing anything:

    python3 scripts/agent_control.py task create --objective "..." \
        --deliverable "runtime registry::src/runtime/registry.py" \
        --validation "static-invariants=python3 scripts/check_static_invariants.py" \
        --validation ci-pr-gates --step "write the registry" --pr

or from a JSON scope file (`objective`, `deliverables`, `validations`,
`steps`, `scope`):

    python3 scripts/agent_control.py task create --scope-file scope.json

Record progress as it happens:

    python3 scripts/agent_control.py task phase IMPLEMENTING
    python3 scripts/agent_control.py step add "write the tests"
    python3 scripts/agent_control.py step done S-0002
    python3 scripts/agent_control.py deliverable set D-0001 IMPLEMENTED
    python3 scripts/agent_control.py note --kind decision "reuse the event bus"
    python3 scripts/agent_control.py note --kind rejected "a second registry"
    python3 scripts/agent_control.py task next "add the migration"
    python3 scripts/agent_control.py checkpoint --reason "before the refactor" --verify

Inspect it at any time:

    python3 scripts/agent_control.py task show
    python3 scripts/agent_control.py task list
    python3 scripts/agent_control.py session-summary

## Evidence

Completion is decided from evidence bound to a commit. A validation that
passed before the last commit says nothing about the last commit, so only
evidence recorded at the current HEAD, on a clean tree, counts.

* A validation with a command is run by the tool, which records the exit
  code (`EXECUTED`):

      python3 scripts/agent_control.py evidence run static-invariants

* A validation that only CI can run (the test suite, lint, coverage) is
  satisfied by the PR's CI notice comment -- the repository's sanctioned
  channel for CI results -- recorded against the SHA CI ran on (`ATTESTED`):

      python3 scripts/agent_control.py evidence ci ci-pr-gates --state success \
          --head-sha <sha> --source <url of the CI notice comment>

## Git safety

While a task is active, these operations need a single-use authorization:
`checkout`, `switch`, `restore`, `reset`, `rebase`, `merge`, `pull`,
`cherry-pick`, `stash` (except `list`/`show`), `clean`, and every force push including
`--force-with-lease`. Granting one takes a pre-mutation checkpoint of the
whole Git state first:

    python3 scripts/agent_control.py git authorize --operation checkout --reason "inspect main"

The PreToolUse hook refuses the operation otherwise and prints the exact
command. Destructive forms (`reset --hard`, `clean -f`, `push --force`) still
need the separate human approval marker on top. Vague instructions ("clean
this up", "go back") authorize nothing.

After every commit (the PostToolUse hook does this automatically for `git
commit`):

    python3 scripts/agent_control.py git verify-commit

Before delivery:

    python3 scripts/agent_control.py git audit-diff
    python3 scripts/agent_control.py git verify-push
    python3 scripts/agent_control.py delivery pr --number <n> --url <url> --head-sha <sha>
    python3 scripts/agent_control.py git final-audit
    python3 scripts/agent_control.py git snapshot

## Branch and worktree audits

An audit is a claim about a branch at a commit; when the branch moves the
entry becomes STALE.

    python3 scripts/agent_control.py branch-audit inventory
    python3 scripts/agent_control.py branch-audit run
    python3 scripts/agent_control.py branch-audit record <branch> --pr 12 --pr-state open --ci failure
    python3 scripts/agent_control.py branch-audit verify

`verify` recomputes the inventory from Git and passes only when inventory
count, ledger count and classified count agree with nothing missing, extra,
stale or reclassified. `--remote-heads` adds the remote's own branch list
(network). A branch whose history is not available locally -- a shallow
clone -- is BLOCKED, not clean: fetch it before claiming anything about it.

## Completion

    python3 scripts/agent_control.py verify-completion
    python3 scripts/agent_control.py task complete

`verify-completion` prints machine-readable JSON with `complete`, `task_id`,
`branch`, `head_sha`, `validated_at` and the exact `blocking_reasons`; it
exits 1 while anything blocks. `task complete` records COMPLETE only if the
validator passes at that moment. The predicates: task ACTIVE; correct
worktree and branch; no merge/rebase/cherry-pick in progress; no tracked
changes; no untracked files; no open checklist item; a verified commit beyond
the baseline at HEAD; every recorded commit still in HEAD's history; every
deliverable at COMMITTED (PUSHED when a push is required) with its paths
changed between baseline and HEAD; passing evidence at HEAD for every
required validation; a final diff audit at HEAD; the remote branch at HEAD
(when a push is required); the PR head at HEAD (when a PR is required); a
reconciled branch audit (when required).

## Stopping, compaction and new sessions

* **Stop hook.** While the active task is ACTIVE and incomplete, the turn may
  not end; the blocking predicates are returned instead. To stop on purpose,
  say why and what comes next:

      python3 scripts/agent_control.py task pause --reason "..." --next-action "..."
      python3 scripts/agent_control.py task block --reason "..." --next-action "..."
      python3 scripts/agent_control.py task fail --reason "..."
      python3 scripts/agent_control.py task abandon --reason "..."

  After five consecutive blocks without progress (a verified commit resets
  the count) the hook pauses the task itself, WIP-aware, with the blockers as
  the reason -- a session is never trapped, and never ends with an ACTIVE
  task silently left behind.
* **PreCompact hook.** A checkpoint (phase, open steps, changed files, HEAD,
  findings, rejected approaches, next action, branch and worktree) is written
  and read back before compaction. If that fails, compaction is blocked.
* **SessionStart hook.** Every new, resumed, cleared or compacted session
  starts with ACTIVE TASK, STATUS/PHASE, BRANCH, HEAD, OPEN BLOCKERS, DIRTY
  FILES, LAST CHECKPOINT, NEXT REQUIRED ACTION and DECISIONS rebuilt from disk.
  Resume explicitly with:

      python3 scripts/agent_control.py task resume <task-id>

Cloud sessions run in ephemeral containers: the state directory lives as long
as the clone does. Anything that must outlive the container has to be
committed and pushed; the PR is the durable record of delivered work.

## Exit codes

`0` done or check passed; `1` the check ran and failed (incomplete task,
unreconciled audit, failing validation, HEAD mismatch); `2` the request was
refused (unknown task, invalid transition, malformed input, Git error).

## Keeping this document honest

    python3 scripts/agent_control.py docs verify

checks every `scripts/agent_control.py` invocation in `CLAUDE.md`, `docs/`,
`.claude/skills/*/SKILL.md` and `.claude/rules/` against the command table;
`tests/agent_control/test_cli.py` runs the same check on every PR.
