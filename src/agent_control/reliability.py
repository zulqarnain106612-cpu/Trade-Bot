"""Durable agent reliability and failure-lineage control.

State is stored in the Git common directory so evidence cannot dirty the
worktree. Completion can require a green CI verdict for the exact HEAD.

Registry: GOV-057, GOV-063.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2])).resolve()
MAX_EVENTS = 200
REPEAT_LIMIT = 3


@dataclass(frozen=True, slots=True)
class ReliabilityResult:
    ok: bool
    blockers: tuple[str, ...] = ()


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _git_common_dir() -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode:
        return ROOT / ".git"
    value = Path(proc.stdout.strip())
    return value if value.is_absolute() else (ROOT / value).resolve()


def state_path() -> Path:
    return _git_common_dir() / "agent-control" / "reliability.json"


@contextlib.contextmanager
def _locked() -> Iterator[Path]:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "events": [], "ci": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "events": [], "ci": {}}
    if not isinstance(data, dict):
        return {"version": 1, "events": [], "ci": {}}
    data.setdefault("version", 1)
    data.setdefault("events", [])
    data.setdefault("ci", {})
    return data


def _save(path: Path, data: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=".reliability-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _clean(value: object, limit: int = 2400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text.strip()[:limit]


def fingerprint(*parts: object) -> str:
    raw = "\x1f".join(_clean(part, 1000) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def record_tool_failure(
    *,
    tool: str,
    summary: str,
    command: str = "",
    task_id: str | None = None,
    head_sha: str | None = None,
) -> dict[str, Any]:
    fp = fingerprint("tool", tool, re.sub(r"\s+", " ", command), summary)
    with _locked() as path:
        data = _load(path)
        previous = [
            event
            for event in data["events"]
            if event.get("fingerprint") == fp and event.get("status") != "resolved"
        ]
        count = len(previous) + 1
        event = {
            "id": f"FAIL-{fp[:8]}-{len(data['events']) + 1}",
            "kind": "tool",
            "status": "repeat" if count >= REPEAT_LIMIT else "open",
            "fingerprint": fp,
            "repeat_count": count,
            "tool": _clean(tool, 200),
            "command": _clean(command, 1000),
            "summary": _clean(summary),
            "task_id": task_id,
            "head_sha": head_sha,
            "created_at": _now(),
        }
        data["events"] = (data["events"] + [event])[-MAX_EVENTS:]
        _save(path, data)
        return event


def record_ci_verdict(
    *,
    sha: str,
    pr: int,
    status: str,
    failed_checks: list[str] | tuple[str, ...] = (),
    notice: str = "",
) -> dict[str, Any]:
    if status not in {"green", "failed", "pending", "unknown"}:
        raise ValueError(f"invalid CI status: {status}")
    with _locked() as path:
        data = _load(path)
        old = data["ci"].get(sha, {})
        record = {
            "sha": sha,
            "pr": int(pr),
            "status": status,
            "failed_checks": [str(value) for value in failed_checks],
            "notice": _clean(notice),
            "updated_at": _now(),
            "attempt": int(old.get("attempt", 0)) + 1,
        }
        data["ci"][sha] = record
        if status == "green":
            for event in data["events"]:
                if event.get("kind") == "ci" and event.get("status") in {"open", "repeat"}:
                    event["status"] = "resolved"
                    event["resolved_by_sha"] = sha
                    event["resolved_at"] = _now()
        else:
            fp = fingerprint("ci", pr, sha, ",".join(record["failed_checks"]))
            previous = [event for event in data["events"] if event.get("fingerprint") == fp]
            data["events"].append(
                {
                    "id": f"CI-{sha[:12]}-{fp[:8]}",
                    "kind": "ci",
                    "status": "repeat" if len(previous) + 1 >= REPEAT_LIMIT else "open",
                    "fingerprint": fp,
                    "repeat_count": len(previous) + 1,
                    "pr": pr,
                    "head_sha": sha,
                    "failed_checks": record["failed_checks"],
                    "summary": _clean(notice),
                    "created_at": _now(),
                }
            )
            data["events"] = data["events"][-MAX_EVENTS:]
        _save(path, data)
        return record


def record_ci_from_notice(*, sha: str, pr: int, notice: str) -> dict[str, Any]:
    lower = notice.lower()
    if "all checks green" in lower:
        return record_ci_verdict(sha=sha, pr=pr, status="green", notice=notice)
    if "no verdict" in lower or "still running" in lower:
        return record_ci_verdict(sha=sha, pr=pr, status="pending", notice=notice)
    failed = [
        f"{match.group(1)} / {match.group(2)}"
        for match in re.finditer(
            r"^\*\*(.+?) / (.+?)\*\* — (?:failure|cancelled|timed_out|neutral|action_required|stale|no verdict)",
            notice,
            re.MULTILINE,
        )
    ]
    return record_ci_verdict(
        sha=sha,
        pr=pr,
        status="failed",
        failed_checks=sorted(set(failed)) or ["non-green CI"],
        notice=notice,
    )


def gate(
    *,
    task_id: str | None,
    head_sha: str | None,
    requires_ci: bool,
) -> ReliabilityResult:
    if not requires_ci:
        return ReliabilityResult(True)
    if not head_sha:
        return ReliabilityResult(False, ("reliability: HEAD unavailable",))
    with _locked() as path:
        data = _load(path)
    ci = data.get("ci", {}).get(head_sha)
    if not ci:
        return ReliabilityResult(
            False,
            (f"reliability: no CI verdict for HEAD {head_sha[:12]}; run local_checks.py prepare",),
        )
    if ci.get("status") != "green":
        return ReliabilityResult(
            False,
            (
                f"reliability: CI for HEAD {head_sha[:12]} is {ci.get('status')}; "
                f"failed checks: {', '.join(ci.get('failed_checks') or ['unknown'])}",
            ),
        )
    repeats = [
        event
        for event in data.get("events", [])
        if event.get("status") == "repeat"
        and event.get("task_id") == task_id
        and event.get("head_sha") == head_sha
    ]
    if repeats:
        return ReliabilityResult(False, ("reliability: repeated failure remains unresolved at HEAD",))
    return ReliabilityResult(True)


def snapshot() -> dict[str, Any]:
    with _locked() as path:
        return _load(path)
