"""
``scripts/agent_control.py`` -- the command-line face of the agent-control layer.

Every subcommand is a thin shell over ``Workspace``: parse, call, print.
Machine-readable answers (``verify-completion``, ``task show``,
``branch-audit verify``, ``git final-audit``) are JSON on stdout.

Exit codes: 0 done / check passed; 1 check ran and failed (incomplete task,
unreconciled audit, docs out of sync); 2 the request itself was refused
(unknown task, invalid transition, malformed input, Git error).

``COMMANDS`` is the single table the parser is built from and ``docs
verify`` checks documentation against, so a documented command that does
not exist -- or was renamed -- fails the check instead of misleading the
next session.

Registry: GOV-060 .. GOV-065 (config/quality_registry.json).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from src.agent_control import branch_audit
from src.agent_control.gitstate import GitError, collect_snapshot
from src.agent_control.model import (
    CompletionState,
    DeliverableState,
    ManifestError,
    TaskPhase,
    TaskScope,
    ValidationRequirement,
    delivery_state,
    from_dict,
    to_dict,
)
from src.agent_control.workspace import Workspace

COMMANDS: dict[str, tuple[str, ...]] = {
    "task": (
        "create",
        "show",
        "list",
        "resume",
        "phase",
        "pause",
        "block",
        "fail",
        "abandon",
        "complete",
        "validate",
        "next",
    ),
    "step": ("add", "done"),
    "deliverable": ("add", "set"),
    "note": (),
    "checkpoint": (),
    "evidence": ("run", "ci"),
    "git": ("snapshot", "authorize", "verify-commit", "audit-diff", "verify-push", "final-audit"),
    "delivery": ("pr",),
    "branch-audit": ("inventory", "run", "record", "verify"),
    "verify-completion": (),
    "session-summary": (),
    "docs": ("verify",),
}

DOC_GLOBS = ("CLAUDE.md", "docs/**/*.md", ".claude/skills/*/SKILL.md", ".claude/rules/*.md")
_NO_TASK_ARGUMENT = frozenset(
    {"task create", "task list", "branch-audit", "session-summary", "docs"}
)
_DOC_COMMAND = re.compile(
    r"(?:^|[`$])[ \t]*(?:python3[ \t]+)?scripts/agent_control\.py[ \t]+"
    r"([a-z][a-z-]*)(?:[ \t]+([a-z][a-z-]*))?",
    re.MULTILINE,
)


class CliError(Exception):
    """A request the CLI refuses before touching any state."""


def _print_json(payload: Any, out: TextIO) -> None:
    out.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _task_view(workspace: Workspace, task_id: str | None) -> dict[str, Any]:
    task = workspace.load(task_id)
    view = to_dict(task)
    view["open_steps"] = [s.step_id for s in task.open_steps]
    view["completed_steps"] = [s.step_id for s in task.completed_steps]
    view["delivery_state"] = str(delivery_state(task))
    return view


def _deliverable_arg(value: str) -> tuple[str, list[str]]:
    description, _, paths = value.partition("::")
    if not description.strip():
        raise CliError(f"deliverable {value!r} has no description")
    return description.strip(), [p.strip() for p in paths.split(",") if p.strip()]


def _validation_arg(value: str) -> ValidationRequirement:
    name, sep, command = value.partition("=")
    if not name.strip() or (sep and not command.strip()):
        raise CliError(f"validation {value!r} must be NAME or NAME=COMMAND")
    return ValidationRequirement(name.strip(), shlex.split(command) if sep else None)


def _scope_file(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"cannot read scope file {path}: {exc}") from exc
    allowed = {"objective", "deliverables", "validations", "steps", "scope"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise CliError(f"scope file must be an object with keys from {sorted(allowed)}")
    return data


def _create(workspace: Workspace, args: argparse.Namespace, out: TextIO) -> int:
    spec = _scope_file(args.scope_file)
    objective = args.objective or spec.get("objective")
    if not objective:
        raise CliError("an objective is required (--objective or the scope file)")
    scope = from_dict(TaskScope, spec.get("scope", {}))
    scope.in_scope_paths.extend(args.path)
    scope.requires_commit = scope.requires_commit and not args.no_commit
    scope.requires_push = scope.requires_push and not args.no_push and not args.no_commit
    scope.requires_pr = scope.requires_pr or args.pr
    scope.requires_branch_audit = scope.requires_branch_audit or args.branch_audit
    deliverables = [(d["description"], d.get("paths", [])) for d in spec.get("deliverables", [])]
    deliverables += [_deliverable_arg(value) for value in args.deliverable]
    validations = [from_dict(ValidationRequirement, v) for v in spec.get("validations", [])]
    validations += [_validation_arg(value) for value in args.validation]
    task = workspace.create_task(
        objective,
        scope=scope,
        deliverables=deliverables,
        validations=validations,
        steps=[*spec.get("steps", []), *args.step],
        baseline_sha=args.baseline,
    )
    out.write(f"{task.task_id}\n")
    return 0


def _task(workspace: Workspace, args: argparse.Namespace, out: TextIO) -> int:
    action = args.action
    if action == "create":
        return _create(workspace, args, out)
    if action == "show":
        _print_json(_task_view(workspace, args.task_id), out)
    elif action == "list":
        for task_id in workspace.store.task_ids():
            task = workspace.store.load(task_id)
            out.write(f"{task_id}\t{task.status}\t{task.phase}\t{task.branch}\t{task.objective}\n")
    elif action == "resume":
        task = workspace.resume(args.task_id)
        out.write(workspace.session_summary() + "\n")
    elif action == "phase":
        workspace.set_phase(args.task_id, TaskPhase(args.phase))
    elif action in ("pause", "block", "fail", "abandon"):
        target = {
            "pause": CompletionState.PAUSED,
            "block": CompletionState.BLOCKED,
            "fail": CompletionState.FAILED,
            "abandon": CompletionState.ABANDONED,
        }[action]
        task = workspace.stop(
            args.task_id, target, reason=args.reason, next_action=args.next_action
        )
        out.write(f"{task.task_id} {task.status}\n")
    elif action == "complete":
        report = workspace.complete(args.task_id, offline=args.offline)
        _print_json(report, out)
        return 0 if report["complete"] else 1
    elif action == "validate":
        task = workspace.load(args.task_id)
        out.write(f"{task.task_id} manifest valid\n")
    else:
        workspace.set_next_action(args.task_id, args.text)
    return 0


def _evidence(workspace: Workspace, args: argparse.Namespace, out: TextIO) -> int:
    if args.action == "run":
        record = workspace.run_validation(args.task_id, args.name)
    else:
        record = workspace.record_ci(
            args.task_id, args.name, state=args.state, head_sha=args.head_sha, reference=args.source
        )
    out.write(f"{record.evidence_id} {'ok' if record.ok else 'FAILED'} {record.subject}\n")
    return 0 if record.ok else 1


def _final_audit(workspace: Workspace, task_id: str | None, out: TextIO) -> int:
    task = workspace.load(task_id)
    snapshot = workspace.snapshot()
    checks = {
        "worktree_matches": snapshot.repository_root == task.worktree,
        "branch_matches": snapshot.branch == task.branch,
        "tree_clean": snapshot.is_clean,
        "no_active_operation": not snapshot.active_operations,
        "head_verified": snapshot.head_sha in {c.sha for c in task.commits},
    }
    _print_json(
        {
            "task_id": task.task_id,
            "branch": snapshot.branch,
            "head_sha": snapshot.head_sha,
            "dirty_paths": snapshot.dirty_paths,
            "active_operations": snapshot.active_operations,
            "worktrees": [to_dict(tree) for tree in snapshot.worktrees],
            "checks": checks,
            "ok": all(checks.values()),
        },
        out,
    )
    return 0 if all(checks.values()) else 1


def _git(workspace: Workspace, args: argparse.Namespace, out: TextIO) -> int:
    action = args.action
    if action == "snapshot":
        if args.task_id:
            point = workspace.checkpoint(args.task_id, "git snapshot")
            _print_json(to_dict(point.git), out)
        else:
            _print_json(to_dict(collect_snapshot(workspace.runner)), out)
    elif action == "authorize":
        grant, point = workspace.authorize(
            args.task_id, args.operation, args.reason, ttl_s=args.ttl_seconds
        )
        out.write(f"{grant} granted for one '{args.operation}' (pre-mutation checkpoint {point})\n")
    elif action == "verify-commit":
        commit = workspace.verify_commit(args.task_id, args.rev)
        out.write(f"{commit.sha} verified (branch head matched: {commit.branch_head_matched})\n")
        return 0 if commit.branch_head_matched else 1
    elif action == "audit-diff":
        record = workspace.audit_diff(args.task_id)
        out.write(f"{record.evidence_id} {'ok' if record.ok else 'FAILED'}\n{record.summary}\n")
        return 0 if record.ok else 1
    elif action == "verify-push":
        record = workspace.verify_push(args.task_id, remote=args.remote)
        out.write(f"{record.evidence_id} {'ok' if record.ok else 'MISMATCH'}: {record.summary}\n")
        return 0 if record.ok else 1
    else:
        return _final_audit(workspace, args.task_id, out)
    return 0


def _branch_audit(workspace: Workspace, args: argparse.Namespace, out: TextIO) -> int:
    action = args.action
    if action == "verify":
        report = workspace.branch_report(
            base=args.base, remote=args.remote, include_remote_heads=args.remote_heads
        )
        _print_json(report, out)
        return 0 if report["reconciled"] else 1
    if action == "record":
        inventory = branch_audit.collect_refs(
            workspace.runner, remote=args.remote, include_remote_heads=args.remote_heads
        )
        current = next((ref.sha for ref in inventory if ref.name == args.branch), None)
        if current is None:
            raise CliError(f"{args.branch!r} is not in the inventory")
        with workspace.store.locked():
            ledger = workspace.store.load_ledger()
            entry = branch_audit.record_attestation(
                ledger,
                args.branch,
                current,
                pr=args.pr,
                pr_state=args.pr_state,
                ci_state=args.ci,
                issue=args.issue,
                action=args.action_taken,
                evidence=args.evidence,
            )
            workspace.store.save_ledger(ledger)
        out.write(f"{entry.branch_name} {entry.completion_status} @ {entry.audited_head_sha}\n")
        return 0
    inventory_list = branch_audit.collect_inventory(
        workspace.runner,
        base=args.base,
        remote=args.remote,
        include_remote_heads=args.remote_heads,
    )
    if action == "inventory":
        rows = [
            {"branch": obs.ref.name, "sha": obs.ref.sha, "history": obs.history}
            for obs in inventory_list
        ]
        _print_json(rows, out)
        return 0
    now = datetime.now(UTC).replace(microsecond=0)
    with workspace.store.locked():
        ledger = branch_audit.run_audit(
            workspace.store.load_ledger(),
            inventory_list,
            audit_id=f"BA-{now:%Y%m%dT%H%M%SZ}",
            now=now.isoformat(),
        )
        workspace.store.save_ledger(ledger)
    counts: dict[str, int] = {}
    for entry in ledger.entries:
        counts[str(entry.completion_status)] = counts.get(str(entry.completion_status), 0) + 1
    _print_json({"audited": len(inventory_list), "by_state": counts}, out)
    return 0


def documented_commands(root: Path) -> list[tuple[str, str, str | None]]:
    """(file, command, subcommand) for every agent_control invocation in the docs."""
    found: list[tuple[str, str, str | None]] = []
    for pattern in DOC_GLOBS:
        for path in sorted(root.glob(pattern)):
            text = path.read_text(encoding="utf-8")
            for match in _DOC_COMMAND.finditer(text):
                found.append((str(path.relative_to(root)), match.group(1), match.group(2)))
    return found


def docs_problems(root: Path) -> list[str]:
    problems: list[str] = []
    for where, command, sub in documented_commands(root):
        if command not in COMMANDS:
            problems.append(f"{where}: unknown command '{command}'")
        elif COMMANDS[command] and sub not in COMMANDS[command]:
            problems.append(f"{where}: '{command}' has no subcommand '{sub}'")
    return problems


def _docs(root: Path, out: TextIO) -> int:
    documented = documented_commands(root)
    problems = docs_problems(root)
    if not documented:
        problems.append("no agent_control commands are documented")
    for problem in problems:
        out.write(problem + "\n")
    out.write(f"{len(documented)} documented invocation(s), {len(problems)} problem(s)\n")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent_control", description=__doc__.split("\n\n")[0])
    parser.add_argument("--cwd", default=".", help="repository (worktree) to operate on")
    top = parser.add_subparsers(dest="command", required=True)
    for command, subs in COMMANDS.items():
        node = top.add_parser(command)
        if subs:
            actions = node.add_subparsers(dest="action", required=True)
            for sub in subs:
                _arguments(command, sub, actions.add_parser(sub))
        else:
            _arguments(command, None, node)
    return parser


def _arguments(command: str, sub: str | None, p: argparse.ArgumentParser) -> None:
    key = f"{command} {sub}" if sub else command
    if key not in _NO_TASK_ARGUMENT and command not in _NO_TASK_ARGUMENT:
        p.add_argument("task_id", nargs="?", help="task id (default: the worktree's active task)")
    if key == "task create":
        p.add_argument("--objective")
        p.add_argument("--scope-file")
        p.add_argument("--deliverable", action="append", default=[], help="DESC[::path,path]")
        p.add_argument("--validation", action="append", default=[], help="NAME or NAME=COMMAND")
        p.add_argument("--step", action="append", default=[])
        p.add_argument("--path", action="append", default=[], help="in-scope path glob")
        p.add_argument("--baseline")
        p.add_argument("--no-commit", action="store_true")
        p.add_argument("--no-push", action="store_true")
        p.add_argument("--pr", action="store_true")
        p.add_argument("--branch-audit", action="store_true")
    elif key == "task phase":
        p.add_argument("phase", choices=[str(phase) for phase in TaskPhase])
    elif key in ("task pause", "task block", "task fail", "task abandon"):
        p.add_argument("--reason", required=True)
        p.add_argument("--next-action")
    elif key in ("task complete", "verify-completion"):
        p.add_argument("--offline", action="store_true")
    elif key == "task next":
        p.add_argument("text")
    elif key in ("step add", "deliverable add", "note"):
        p.add_argument("text")
        if command == "deliverable":
            p.add_argument("--path", action="append", default=[])
        if command == "note":
            p.add_argument("--kind", choices=["finding", "rejected", "decision"], required=True)
    elif key == "step done":
        p.add_argument("step_id")
        p.add_argument("--evidence", action="append", default=[])
    elif key == "deliverable set":
        p.add_argument("deliverable_id")
        p.add_argument("state", choices=[str(state) for state in DeliverableState])
        p.add_argument("--regress", action="store_true")
    elif key == "checkpoint":
        p.add_argument("--reason", default="manual checkpoint")
        p.add_argument("--verify", action="store_true")
    elif key in ("evidence run", "evidence ci"):
        p.add_argument("name")
        if sub == "ci":
            p.add_argument("--state", required=True, choices=["success", "failure", "pending"])
            p.add_argument("--head-sha", required=True)
            p.add_argument("--source", required=True, help="URL of the PR CI notice comment")
    elif key == "git authorize":
        p.add_argument("--operation", required=True)
        p.add_argument("--reason", required=True)
        p.add_argument("--ttl-seconds", type=int, default=900)
    elif key == "git verify-commit":
        p.add_argument("--rev", default="HEAD")
    elif key == "git verify-push":
        p.add_argument("--remote", default="origin")
    elif key == "delivery pr":
        p.add_argument("--number", type=int, required=True)
        p.add_argument("--url", required=True)
        p.add_argument("--head-sha", required=True)
    elif command == "branch-audit":
        p.add_argument("--base", default="origin/main")
        p.add_argument("--remote", default="origin")
        p.add_argument("--remote-heads", action="store_true", help="also list the remote (network)")
        if sub == "record":
            p.add_argument("branch")
            p.add_argument("--pr", type=int)
            p.add_argument("--pr-state", choices=sorted(branch_audit.PR_STATES))
            p.add_argument("--ci", choices=sorted(branch_audit.CI_STATES))
            p.add_argument("--issue", type=int)
            p.add_argument("--action-taken")
            p.add_argument("--evidence")


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    stdout = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        if args.command == "docs":
            return _docs(Path(args.cwd).resolve(), stdout)
        workspace = Workspace.open(args.cwd)
        return _dispatch(workspace, args, stdout)
    except (CliError, ManifestError, GitError, ValueError) as exc:
        sys.stderr.write(f"agent_control: error: {exc}\n")
        return 2


def _dispatch(workspace: Workspace, args: argparse.Namespace, out: TextIO) -> int:
    command = args.command
    if command == "task":
        return _task(workspace, args, out)
    if command == "step":
        if args.action == "add":
            step = workspace.add_step(args.task_id, args.text)
        else:
            step = workspace.finish_step(args.task_id, args.step_id, evidence_ids=args.evidence)
        out.write(f"{step.step_id} {step.state} {step.text}\n")
    elif command == "deliverable":
        if args.action == "add":
            item = workspace.add_deliverable(args.task_id, args.text, args.path)
        else:
            state = DeliverableState(args.state)
            item = workspace.set_deliverable(
                args.task_id, args.deliverable_id, state, allow_regress=args.regress
            )
        out.write(f"{item.deliverable_id} {item.state} {item.description}\n")
    elif command == "note":
        workspace.add_note(args.task_id, args.kind, args.text)
    elif command == "checkpoint":
        point = workspace.checkpoint(args.task_id, args.reason)
        if args.verify and workspace.verify_checkpoint(args.task_id) != point:
            raise ManifestError(f"checkpoint {point.checkpoint_id} did not read back intact")
        out.write(f"{point.checkpoint_id} {point.created_at} {point.reason}\n")
    elif command == "evidence":
        return _evidence(workspace, args, out)
    elif command == "git":
        return _git(workspace, args, out)
    elif command == "delivery":
        record = workspace.record_pr(
            args.task_id, number=args.number, url=args.url, head_sha=args.head_sha
        )
        verdict = "ok" if record.ok else "HEAD MISMATCH"
        out.write(f"{record.evidence_id} {verdict} {record.summary}\n")
        return 0 if record.ok else 1
    elif command == "branch-audit":
        return _branch_audit(workspace, args, out)
    elif command == "verify-completion":
        report = workspace.verify_completion(args.task_id, offline=args.offline)
        _print_json(report, out)
        return 0 if report["complete"] else 1
    else:
        out.write((workspace.session_summary() or "no active task in this worktree") + "\n")
    return 0
