#!/usr/bin/env python3
"""
PostToolUse hook: the quality contract, applied without being asked for.

A skill has to be invoked, and the moment it is most needed is the moment
nobody thinks to invoke it -- a quick fix at the end of a long session, a
config edit that looks trivial. This hook closes that gap for the cases where
an edit can be checked mechanically in milliseconds:

  * a JSON file that no longer parses, or no longer validates against its
    schema. Caught here, this costs one message; caught in CI it costs twenty
    minutes, and caught in production it costs an outage at startup.
  * the requirements registry edited into a shape the schema rejects -- a
    `verified` status with no test, a `planned_in` left on a shipped entry.
  * a workflow whose gate job no longer needs every job, which is the hole
    that lets a failing check sit behind a green required one.
  * a change under `src/` with no corresponding registry or test change in the
    session. That one is advisory: the hook cannot know whether the entry is
    coming in the next edit, and a hook that blocks on a guess is a hook
    somebody turns off.

Contract (PostToolUse):
  stdin:  {"tool_name": ..., "tool_input": {"file_path": ...}, ...}
  stdout: {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                  "additionalContext": str}}
          or {"decision": "block", "reason": str} when the edit left a file
          in a state that is definitely broken.

It fails **open**. A hook that fails closed on its own bug blocks all work and
looks like a broken environment; this one says so on stderr, which reaches the
transcript, and allows the edit.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR", Path(__file__).resolve().parents[2]))
SKILL_DIR = PROJECT_DIR / ".claude" / "skills" / "quality-engineering"
GATE = SKILL_DIR / "scripts" / "qe_gate.py"
CONFIG = SKILL_DIR / "qe.config.json"

# The hook is meant to be unnoticeable. Anything slower than this is a cost
# paid on every edit, which is how a useful check becomes an annoying one.
TIMEOUT_S = 20

DISABLE_ENV = "TB_QUALITY_HOOK"


def _emit(context: str = "", block: str = "") -> None:
    payload: dict[str, Any] = {}
    if block:
        payload["decision"] = "block"
        payload["reason"] = block
    if context:
        payload["hookSpecificOutput"] = {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    if payload:
        json.dump(payload, sys.stdout)
        sys.stdout.write("\n")
    sys.exit(0)


def _fail_open(note: str) -> None:
    print(f"[quality_gate] degraded, allowing: {note}", file=sys.stderr)
    sys.exit(0)


def _run_gate(only: str) -> dict[str, Any] | None:
    """Run one gate and return its JSON result, or None if it could not run."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, str(GATE), "--only", only, "--json"],
            capture_output=True,
            text=True,
            cwd=PROJECT_DIR,
            timeout=TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return json.loads(proc.stdout or "{}")
    except ValueError:
        return None


def _findings(result: dict[str, Any] | None) -> list[str]:
    if not result:
        return []
    out: list[str] = []
    for entry in result.get("results", []):
        if entry.get("passed") or entry.get("skipped"):
            continue
        out.extend(entry.get("findings") or [f"{entry.get('gate')}: {entry.get('detail')}"])
    return out


def _matches(relative: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in patterns)


def main() -> None:
    if os.environ.get(DISABLE_ENV, "").lower() in {"0", "off", "false"}:
        sys.exit(0)

    try:
        event = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001 - malformed input is not the edit's fault
        _fail_open(f"unreadable hook input: {exc}")
        return

    tool = event.get("tool_name", "")
    if tool not in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        sys.exit(0)

    raw_path = (event.get("tool_input") or {}).get("file_path")
    if not raw_path:
        sys.exit(0)

    path = Path(raw_path)
    try:
        relative = path.resolve().relative_to(PROJECT_DIR.resolve()).as_posix()
    except (ValueError, OSError):
        sys.exit(0)  # outside the project: not ours to judge

    if not GATE.exists():
        _fail_open("the quality-engineering skill is not present on this branch")
        return

    notes: list[str] = []
    blockers: list[str] = []

    # --- a JSON file that no longer parses is definitely broken -------------
    if relative.endswith(".json") and path.exists():
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            _emit(block=f"{relative} is no longer valid JSON: {exc}. Fix it before moving on.")
            return
        except OSError as exc:
            _fail_open(f"cannot read {relative}: {exc}")
            return

    # --- the registry and the schema'd configs ------------------------------
    if relative in {"config/quality_registry.json", "config/quality_registry.schema.json"}:
        findings = _findings(_run_gate("registry-schema")) + _findings(_run_gate("registry-loader"))
        if findings:
            blockers.append(
                "The requirements registry no longer satisfies its contract:\n  - "
                + "\n  - ".join(findings[:6])
                + "\n\nThe rules are in config/quality_registry.schema.json: a verified or "
                "partial entry names at least one test that exists; a planned entry names "
                "a phase and no test; a critical entry is never an accepted gap."
            )
        else:
            notes.append(
                "Registry still valid. If this change alters what is verified, regenerate "
                "the traceability document: python3 scripts/generate_quality_docs.py"
            )
    elif relative.startswith("config/") and relative.endswith(".json"):
        findings = _findings(_run_gate("json-validity"))
        if findings:
            blockers.append(
                "A configuration file does not validate against its schema:\n  - "
                + "\n  - ".join(findings[:5])
            )

    # --- workflows ----------------------------------------------------------
    elif relative.startswith(".github/workflows/"):
        findings = _findings(_run_gate("workflow-gates"))
        if findings:
            blockers.append(
                "A workflow that runs on pull requests has a gate problem:\n  - "
                + "\n  - ".join(findings[:5])
                + "\n\nA job outside the gate's `needs:` can fail while the required check "
                "stays green, which is the hole the gate pattern closes."
            )

    # --- production code: advisory ------------------------------------------
    elif relative.startswith("src/") and relative.endswith(".py"):
        try:
            config = json.loads(CONFIG.read_text(encoding="utf-8"))
            patterns = config["change_classes"]["production_behaviour"]["matches"]
            why = config["change_classes"]["production_behaviour"]["why"]
        except Exception:  # noqa: BLE001 - advisory path, never block on it
            patterns, why = ["src/**/*.py"], ""
        if _matches(relative, patterns):
            notes.append(
                f"{relative} is production behaviour. {why} Before this change is done: "
                "which registry id does it serve, which test decides it, and what goes "
                "wrong in production if it is wrong? "
                "Scaffold an entry with .claude/skills/quality-engineering/scripts/"
                "qe_new_requirement.py; run scripts/qe_gate.py before pushing."
            )

    if blockers:
        _emit(block="\n\n".join(blockers))
    elif notes:
        _emit(context="\n".join(notes))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - see _fail_open
        _fail_open(f"{type(exc).__name__}: {exc}")
