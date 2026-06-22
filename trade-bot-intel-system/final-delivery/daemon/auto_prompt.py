#!/usr/bin/env python3
"""
Auto Prompt Builder
====================
Wraps your raw message with project context automatically.
Three integration modes:

MODE 1 — CLI wrapper (intercepts any command-line agent call)
  Instead of: claude "implement entropy gate"
  You run:    intel-prompt "implement entropy gate"
  This script prepends the full context primer before sending.

MODE 2 — Clipboard auto-prepend
  Run: intel-prompt --clipboard
  Opens your message via $EDITOR, then copies the full context-prepended
  version to clipboard. Paste into any chat UI.

MODE 3 — Watch mode (daemon sub-process)
  Watches a "prompt drop" file. You write your message to:
    .project-intel/.pending_prompt
  Script auto-wraps it and writes to:
    .project-intel/.ready_prompt
  IDE extension or browser extension reads .ready_prompt.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path


def find_project_root(start: Path = None) -> Path | None:
    """Walk up from cwd to find .project-intel directory."""
    check = (start or Path.cwd()).resolve()
    while check != check.parent:
        if (check / ".project-intel").exists():
            return check
        check = check.parent
    return None


def load_primer(project_root: Path) -> str:
    primer_file = project_root / ".project-intel" / "CONTEXT_PRIMER.md"
    if primer_file.exists():
        return primer_file.read_text()
    return ""


def load_session_state(project_root: Path) -> dict:
    state_file = project_root / ".project-intel" / "SESSION_STATE.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except Exception:
            pass
    return {}


def load_recent_decisions(project_root: Path, max_chars: int = 800) -> str:
    dl_file = project_root / ".project-intel" / "DECISION_LOG.md"
    if not dl_file.exists():
        return ""
    content = dl_file.read_text()
    # Only last N chars to stay compact
    return content[-max_chars:] if len(content) > max_chars else content


def build_context_block(project_root: Path, user_message: str) -> str:
    """Build the full auto-prepended prompt."""
    primer = load_primer(project_root)
    state = load_session_state(project_root)
    recent_decisions = load_recent_decisions(project_root)

    current_focus = state.get("current_focus", "not set")
    next_task = state.get("next_recommended_task", "check OPEN_TASKS.md")
    last_modified = state.get("last_files_modified", [])
    last_modified_str = ", ".join(last_modified) if last_modified else "none"
    session_count = state.get("total_sessions", 0)
    last_extraction = state.get("last_extraction", "unknown")

    context = f"""[AUTO-INJECTED PROJECT CONTEXT — session #{session_count}]
[Intel last updated: {last_extraction}]

{primer}

[CURRENT SESSION STATE]
Focus: {current_focus}
Next task: {next_task}
Last modified files: {last_modified_str}

[RECENT DECISIONS]
{recent_decisions}

[END AUTO-INJECTED CONTEXT]

─────────────────────────────────────────
YOUR TASK:
{user_message}
─────────────────────────────────────────

RULES: Read the context above. Do NOT read source files unless modifying a specific one.
Use .project-intel/MODULE_MAP.json for structural questions.
After completing work, output the update_session.py command for me to run.
"""
    return context


def update_state_on_send(project_root: Path, message: str):
    """Record that a prompt was sent."""
    state_file = project_root / ".project-intel" / "SESSION_STATE.json"
    try:
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        state["last_prompt_sent"] = datetime.now().isoformat()
        state["last_prompt_preview"] = message[:100]
        state["session_status"] = "waiting_for_response"
        state_file.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def mode_cli(args):
    """Mode 1: Direct CLI — prepend context and pass to agent command."""
    project_root = find_project_root()
    if not project_root:
        # No project found — pass through unmodified
        os.execvp(args.command[0], args.command)
        return

    user_message = " ".join(args.message) if args.message else ""
    if not user_message and not sys.stdin.isatty():
        user_message = sys.stdin.read().strip()

    full_prompt = build_context_block(project_root, user_message)
    update_state_on_send(project_root, user_message)

    if args.command:
        # Pass to agent CLI (e.g. `claude`, `aider`, `ollama run llama3`)
        proc = subprocess.run(
            args.command,
            input=full_prompt,
            text=True
        )
        sys.exit(proc.returncode)
    else:
        # Just print the built prompt (for pipe usage)
        print(full_prompt)


def mode_clipboard(project_root: Path, message: str):
    """Mode 2: Build prompt and copy to clipboard."""
    full_prompt = build_context_block(project_root, message)
    update_state_on_send(project_root, message)

    # Try xclip, xsel, wl-copy in order
    for cmd in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["wl-copy"]]:
        try:
            proc = subprocess.run(cmd, input=full_prompt, text=True, capture_output=True)
            if proc.returncode == 0:
                print(f"✓ Full context prompt copied to clipboard ({len(full_prompt)} chars)")
                print(f"  Paste into Claude.ai, Copilot chat, or any agent UI")
                return
        except FileNotFoundError:
            continue

    # Fallback: write to temp file
    tmp = Path("/tmp/intel_prompt.txt")
    tmp.write_text(full_prompt)
    print(f"✓ Full context prompt written to: {tmp}")
    print(f"  (Install xclip for clipboard: sudo apt install xclip)")


def mode_watch(project_root: Path):
    """
    Mode 3: Watch .pending_prompt file.
    Write your message there → auto-wrapped prompt appears in .ready_prompt.
    """
    pending = project_root / ".project-intel" / ".pending_prompt"
    ready   = project_root / ".project-intel" / ".ready_prompt"

    print(f"Watch mode active. Write your message to:")
    print(f"  {pending}")
    print(f"Auto-wrapped prompt will appear at:")
    print(f"  {ready}")
    print("Ctrl+C to stop.\n")

    last_mtime = 0.0
    while True:
        try:
            if pending.exists():
                mtime = pending.stat().st_mtime
                if mtime > last_mtime:
                    last_mtime = mtime
                    message = pending.read_text().strip()
                    if message:
                        full_prompt = build_context_block(project_root, message)
                        ready.write_text(full_prompt)
                        update_state_on_send(project_root, message)
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] Prompt built → {ready.name}")
                        # Also copy to clipboard if possible
                        for cmd in [["xclip", "-selection", "clipboard"], ["wl-copy"]]:
                            try:
                                proc = subprocess.run(cmd, input=full_prompt, text=True, capture_output=True)
                                if proc.returncode == 0:
                                    print(f"  Copied to clipboard automatically")
                                    break
                            except FileNotFoundError:
                                continue
            time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nWatch mode stopped")
            break


def main():
    parser = argparse.ArgumentParser(
        description="Auto-build context-aware prompts for any AI agent"
    )
    parser.add_argument("message", nargs="*", help="Your task/message to the agent")
    parser.add_argument("--clipboard", "-c", action="store_true",
                        help="Copy built prompt to clipboard instead of printing")
    parser.add_argument("--watch", "-w", action="store_true",
                        help="Watch mode — auto-build prompts from .pending_prompt file")
    parser.add_argument("--command", nargs=argparse.REMAINDER,
                        help="Agent CLI to pipe prompt into (e.g. --command claude --command aider)")
    parser.add_argument("--project", help="Explicit project root (auto-detected if not set)")
    args = parser.parse_args()

    project_root = Path(args.project) if args.project else find_project_root()
    if not project_root:
        # No project — just pass message through
        print(" ".join(args.message or []))
        return

    if args.watch:
        mode_watch(project_root)
        return

    user_message = " ".join(args.message) if args.message else ""
    if not user_message and not sys.stdin.isatty():
        user_message = sys.stdin.read().strip()

    if args.clipboard:
        mode_clipboard(project_root, user_message)
    elif args.command:
        mode_cli(args)
    else:
        # Default: print full prompt to stdout
        print(build_context_block(project_root, user_message))


if __name__ == "__main__":
    main()
