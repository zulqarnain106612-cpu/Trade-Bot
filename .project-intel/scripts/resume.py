#!/usr/bin/env python3
"""
SESSION RESUME — single command, zero follow-up file reads.
Target: ≤800 tokens. Every token here costs context window space.
"""
import json, subprocess, sys
from datetime import datetime
from pathlib import Path


def git_status(root):
    try:
        out = subprocess.check_output(["git","status","--short"], cwd=root, text=True, timeout=5).strip()
        return out[:300] if out else "clean"
    except Exception:
        return "unknown"


def git_log(root, n=2):
    try:
        return subprocess.check_output(["git","log",f"-{n}","--oneline"], cwd=root, text=True, timeout=5).strip()
    except Exception:
        return ""


def load_slim_map(intel):
    p = intel / "MODULE_MAP_SLIM.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    intel = root / ".project-intel"

    state = {}
    try:
        state = json.loads((intel / "SESSION_STATE.json").read_text())
    except Exception:
        pass

    handoff       = state.get("handoff", {})
    impl          = state.get("implementation_status", {})
    decisions     = state.get("open_decisions", [])
    next_task     = handoff.get("next_step") or state.get("next_recommended_task", "check OPEN_TASKS.md")
    interruption  = handoff.get("interruption_reason", "")
    files_touched = handoff.get("files_touched", [])
    last_cp       = handoff.get("last_checkpoint", "")
    coverage      = state.get("coverage_pct", "?")
    sessions      = state.get("total_sessions", 0)
    health        = state.get("project_health", "")

    status  = git_status(root)
    commits = git_log(root, 2)
    slim    = load_slim_map(intel)
    now     = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"╔══ TRADE-BOT RESUME [{now}] Session #{sessions} ══╗",
        f"▶ NEXT: {next_task[:160]}",
    ]

    if interruption:
        lines.append(f"⚠ {interruption}")
    if files_touched:
        lines.append(f"  in-flight: {', '.join(files_touched[:8])}")
    if last_cp:
        lines.append(f"  checkpoint: {last_cp}")

    lines += [
        f"HEALTH: {health[:120]}",
        f"COVER:  {coverage}% (gate=60%)",
        "",
        "GIT:",
    ]
    for l in status.splitlines()[:8]:
        lines.append(f"  {l}")
    if commits:
        for l in commits.splitlines():
            lines.append(f"  {l}")

    # Implementation status — key: one-word status only (no long descriptions)
    if impl:
        lines.append("\nSTATUS:")
        # Group by state
        done  = [k for k,v in impl.items() if "COMPLETE" in str(v).upper() or str(v).strip().lower()=="complete"]
        other = {k: str(v)[:60] for k,v in impl.items() if k not in done}
        lines.append(f"  ✓ complete: {', '.join(done)}")
        for k,v in other.items():
            lines.append(f"  ⚡ {k}: {v}")

    # Open decisions — first line only, capped at 3
    if decisions:
        lines.append("\nDECISIONS (do not re-debate):")
        for d in decisions[:3]:
            lines.append(f"  • {str(d)[:100]}")
        if len(decisions) > 3:
            lines.append(f"  • (+{len(decisions)-3} more — see DECISION_LOG.md)")

    # Module map — src/ only, skip __init__.py, 1 line each, purpose truncated to 55 chars
    if slim:
        lines.append("\nSRC MAP:")
        for fp, info in slim.items():
            if fp.endswith("__init__.py"):
                continue  # zero value
            purpose = (info.get("purpose","") if isinstance(info,dict) else str(info))[:55]
            # strip package path noise, keep just filename
            short_fp = fp.replace("src/","")
            lines.append(f"  {short_fp:<40} {purpose}")

    lines += [
        "",
        "FLOW: exchange→fetcher→storage→features→regime→models→filters→sizing→gates→executor→api",
        "GATES: DD>2%|losses≥3|regime=volatile|pos>5%|paper<30d — KELLY×0.5 ceil 0.25",
        "",
        "RULES:",
        "  1. This IS your full context. Read NO other files to orient.",
        "  2. Read src file ONLY immediately before editing it.",
        "  2a. Never cat files >100L — use grep/sed/head/tail.",
        "  3. Checkpoint: python3 .project-intel/scripts/handoff.py checkpoint --agent claude \\",
        '       --completed "X" --next "Y" --files "src/f.py"',
        "  4. Commit: bash scripts/claude-commit.sh --msg 'type(scope): desc'",
        "  NEVER read: MODULE_MAP.json|ARCHITECTURE.md|RAW_SCAN.json|SESSION_STATE.json|GAPS.md",
        "  TAG outputs: <gap><issue><broken><missing><decision><task><risk><debt><chat>",
    ]

    print("\n".join(lines))

    # Update session counter
    try:
        state["total_sessions"] = int(sessions or 0) + 1
        state["last_session_start"] = datetime.now().isoformat()
        state["session_status"] = "active"
        (intel / "SESSION_STATE.json").write_text(json.dumps(state, indent=2))
    except Exception:
        pass


if __name__ == "__main__":
    main()
