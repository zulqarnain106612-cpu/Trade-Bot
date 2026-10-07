"""
Claude Code hook entry points for the agent-control layer.

Each function takes the hook's parsed JSON payload and returns what the thin
wrapper in ``.claude/hooks/`` writes back. They are importable functions, not
scripts, so the suite exercises them without spawning a process (GOV-016).

* ``stop``          Stop. While the worktree's active task is ACTIVE and the
                    completion validator fails, the turn may not end: the
                    exact blocking predicates are returned. A task that passes
                    is recorded COMPLETE. After ``MAX_STOP_BLOCKS``
                    consecutive blocks the task is paused (WIP-aware) with the
                    blockers as its reason -- a session is never trapped, and
                    never ends with an ACTIVE task silently left behind.
* ``precompact``    PreCompact. Persists a checkpoint and re-reads it; if
                    either fails, compaction is blocked so the task state is
                    not lost along with the context.
* ``session_start`` SessionStart. The recovery summary, rebuilt from disk.
* ``post_bash``     PostToolUse(Bash). A ``git commit`` is verified and
                    recorded against the active task.
* ``git_guard``     Called by the repository PreToolUse hook: guarded Git
                    operations that have no task authorization.

No active task in the worktree makes every hook a no-op, so sessions that do
not use the protocol are unaffected. Apart from PreCompact's deliberate
block, an internal failure fails open with a note on stderr: a broken control
layer must not wedge the session it is meant to protect.

Registry: GOV-063, GOV-064 (config/quality_registry.json).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from typing import Any, TextIO

from src.agent_control.git_safety import git_invocations
from src.agent_control.gitstate import GitError
from src.agent_control.model import CompletionState
from src.agent_control.workspace import Workspace

MAX_STOP_BLOCKS = 5
CLI = "python3 scripts/agent_control.py"


def _workspace(event: Mapping[str, Any]) -> Workspace | None:
    try:
        return Workspace.open(str(event.get("cwd") or os.getcwd()))
    except GitError:
        return None


def stop(event: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any] | None:
    workspace = _workspace(event)
    task_id = workspace.active_task_id() if workspace else None
    if workspace is None or task_id is None:
        return None
    if workspace.store.load(task_id).status is not CompletionState.ACTIVE:
        return None
    report = workspace.complete(task_id, now=now)
    if report["complete"]:
        return None
    blockers: list[str] = report["blocking_reasons"]
    with workspace.editing(task_id, now=now) as (task, _):
        task.stop_blocks += 1
        forced = task.stop_blocks > MAX_STOP_BLOCKS
    if forced:
        summary = "; ".join(blockers)
        workspace.stop(
            task_id,
            CompletionState.PAUSED,
            reason=f"stop forced after {MAX_STOP_BLOCKS} completion-gate blocks: {summary}",
            next_action=f"resolve: {blockers[0]}",
            now=now,
        )
        return None
    lines = [
        f"Task {task_id} is ACTIVE and not complete. Blocking predicates:",
        *(f"- {blocker}" for blocker in blockers),
        "Finish the work, or stop explicitly with a reason and the next action:",
        f"  {CLI} task pause {task_id} --reason '...' --next-action '...'",
        f"  {CLI} task block {task_id} --reason '...' --next-action '...'",
    ]
    return {"decision": "block", "reason": "\n".join(lines)}


def precompact(event: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any] | None:
    workspace = _workspace(event)
    if workspace is None:
        return None
    task_id: str | None = None
    try:
        task_id = workspace.active_task_id()
        if task_id is None:
            return None
        point = workspace.checkpoint(task_id, f"pre-compact ({event.get('trigger', '?')})", now=now)
        if workspace.verify_checkpoint(task_id).checkpoint_id != point.checkpoint_id:
            raise OSError("the checkpoint was not on disk when re-read")
    except Exception as exc:  # any failure here means the state was not persisted
        return {
            "decision": "block",
            "reason": (
                f"agent-control could not persist task {task_id or '(active task)'} before "
                f"compaction ({type(exc).__name__}: {exc}). Compaction is blocked so the task "
                "state is not lost; fix the store or pause the task, then compact again."
            ),
        }
    return None


def session_start(event: Mapping[str, Any]) -> str:
    workspace = _workspace(event)
    if workspace is None:
        return ""
    summary = workspace.session_summary()
    if not summary:
        return ""
    source = event.get("source", "startup")
    return f"[agent-control] task state recovered from disk (session {source})\n{summary}"


def post_bash(event: Mapping[str, Any], *, now: datetime | None = None) -> None:
    command = (event.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str) or "commit" not in command:
        return
    try:
        commits = [inv for inv in git_invocations(command) if inv.subcommand == "commit"]
    except ValueError:
        return
    workspace = _workspace(event) if commits else None
    task_id = workspace.active_task_id() if workspace else None
    if workspace is None or task_id is None:
        return
    if workspace.store.load(task_id).status is CompletionState.ACTIVE:
        workspace.verify_commit(task_id, now=now)


def git_guard(event: Mapping[str, Any], *, now: datetime | None = None) -> str | None:
    command = (event.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str) or "git" not in command:
        return None
    workspace = _workspace(event)
    if workspace is None:
        return None
    missing = workspace.guard(command, now=now)
    if not missing:
        return None
    task_id = workspace.active_task_id()
    grants = " ; ".join(
        f"{CLI} git authorize {task_id} --operation {op} --reason '<why>'" for op in missing
    )
    return (
        f"Guarded Git operation(s) {', '.join(missing)} need an explicit authorization on "
        f"active task {task_id} (a pre-mutation checkpoint is taken when it is granted): {grants}"
    )


def run_hook(name: str, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    """Wrapper body for ``.claude/hooks/*``: read the payload, act, answer, fail open."""
    try:
        event = json.loads(stdin.read() or "{}")
        if not isinstance(event, dict):
            raise ValueError("payload is not a JSON object")
        if name == "stop":
            result = stop(event)
        elif name == "precompact":
            result = precompact(event)
        elif name == "session-start":
            text = session_start(event)
            if text:
                stdout.write(text + "\n")
            return 0
        elif name == "post-bash":
            post_bash(event)
            return 0
        else:
            raise ValueError(f"unknown hook {name!r}")
    except Exception as exc:
        stderr.write(f"[agent-control] {name} hook degraded, allowing: {exc!r}\n")
        return 0
    if result is not None:
        stdout.write(json.dumps(result) + "\n")
    return 0
