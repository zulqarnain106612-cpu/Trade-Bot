#!/usr/bin/env bash
# deploy_diagnostics.sh
# 1. Writes scripts/export_diagnostics.py — runs ruff + pyright (Python) and
#    eslint (frontend, if present), writes DIAGNOSTICS.md at project root in
#    bullet-only format mirroring the VS Code Problems panel exactly.
# 2. Writes scripts/gen_project_summary.py — AST-based bullet-only
#    PROJECT_SUMMARY.md generator (skips if file exists, unless --force).
# 3. Generates PROJECT_SUMMARY.md now if it does not already exist.
# 4. Appends a directive block to .claude/CLAUDE.md instructing Claude to
#    load PROJECT_SUMMARY.md and DIAGNOSTICS.md as context.
#
# Usage:
#   bash deploy_diagnostics.sh [project_root]
# Re-run anytime to refresh DIAGNOSTICS.md (cheap, always overwrites).
# PROJECT_SUMMARY.md is NOT overwritten unless you run:
#   python3 scripts/gen_project_summary.py . --force

set -euo pipefail

ROOT="${1:-$(pwd)}"
mkdir -p "$ROOT/scripts" "$ROOT/.claude"

# ───────────────────────── scripts/export_diagnostics.py ─────────────────
cat > "$ROOT/scripts/export_diagnostics.py" << 'PYEOF'
#!/usr/bin/env python3
"""Run ruff + pyright (+ eslint if frontend/ exists) and write DIAGNOSTICS.md
at project root in bullet-only format. Mirrors VS Code's Problems panel,
because VS Code's Python/Pylance/ESLint extensions surface results from
these exact same CLIs under the hood.

Always overwrites DIAGNOSTICS.md — re-run after every save to refresh.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path


def _run(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=300, check=False
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]} timed out after 300s"


def _ruff(root: Path) -> list[str]:
    if shutil.which("ruff") is None:
        return ["- ruff: NOT INSTALLED — `pip install ruff` to enable this section"]
    targets = [t for t in ("src", "tests") if (root / t).exists()]
    if not targets:
        return ["- ruff: no src/ or tests/ directory found to scan"]
    code, out, err = _run(["ruff", "check", *targets, "--output-format=json"], root)
    if code == 127:
        return [f"- ruff: {err}"]
    try:
        diagnostics = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        return [f"- ruff: failed to parse output — {err[:200]}"]
    if not diagnostics:
        return ["- ruff: 0 issues"]
    by_file: dict[str, list[dict]] = defaultdict(list)
    for d in diagnostics:
        rel = str(Path(d["filename"]).resolve()).replace(str(root.resolve()) + "/", "")
        by_file[rel].append(d)
    lines = [f"- ruff: {len(diagnostics)} issue(s) across {len(by_file)} file(s)"]
    for rel in sorted(by_file):
        for d in by_file[rel]:
            row = d["location"]["row"]
            col = d["location"]["column"]
            lines.append(f"  - `{rel}:{row}:{col}` [{d['code']}] {d['message']}")
    return lines


def _pyright(root: Path) -> list[str]:
    if shutil.which("pyright") is None:
        return ["- pyright: NOT INSTALLED — `pip install pyright` to enable this section"]
    targets = [t for t in ("src", "tests") if (root / t).exists()]
    if not targets:
        return ["- pyright: no src/ or tests/ directory found to scan"]
    code, out, err = _run(["pyright", "--outputjson", *targets], root)
    if code == 127:
        return [f"- pyright: {err}"]
    try:
        data = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        return [f"- pyright: failed to parse output — {err[:200]}"]
    diags = data.get("generalDiagnostics", [])
    summary = data.get("summary", {})
    if not diags:
        return ["- pyright: 0 issues"]
    by_file: dict[str, list[dict]] = defaultdict(list)
    for d in diags:
        rel = str(Path(d["file"]).resolve()).replace(str(root.resolve()) + "/", "")
        by_file[rel].append(d)
    lines = [
        f"- pyright: {summary.get('errorCount', 0)} error(s), "
        f"{summary.get('warningCount', 0)} warning(s) across {len(by_file)} file(s)"
    ]
    for rel in sorted(by_file):
        for d in by_file[rel]:
            line = d["range"]["start"]["line"] + 1
            col = d["range"]["start"]["character"] + 1
            rule = d.get("rule", "")
            rule_str = f" ({rule})" if rule else ""
            lines.append(f"  - `{rel}:{line}:{col}` [{d['severity']}]{rule_str} {d['message']}")
    return lines


def _eslint(root: Path) -> list[str]:
    frontend = root / "frontend"
    if not frontend.exists():
        return []
    if shutil.which("npx") is None:
        return ["- eslint: NOT AVAILABLE — npx not on PATH"]
    code, out, err = _run(
        ["npx", "--no-install", "eslint", "src", "--format=json"], frontend
    )
    if code == 127 or "could not determine executable" in err.lower():
        return ["- eslint: NOT INSTALLED in frontend/ — `npm install eslint` to enable"]
    try:
        results = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        return [f"- eslint: failed to parse output — {err[:200]}"]
    total = sum(len(r.get("messages", [])) for r in results)
    if total == 0:
        return ["- eslint: 0 issues"]
    lines = [f"- eslint: {total} issue(s)"]
    for r in results:
        if not r.get("messages"):
            continue
        rel = "frontend/" + str(Path(r["filePath"]).resolve()).replace(
            str(frontend.resolve()) + "/", ""
        )
        for m in r["messages"]:
            sev = "error" if m.get("severity") == 2 else "warning"
            rule = m.get("ruleId") or "syntax"
            lines.append(f"  - `{rel}:{m.get('line')}:{m.get('column')}` [{sev}] ({rule}) {m.get('message')}")
    return lines


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    out: list[str] = ["# DIAGNOSTICS.md", "",
                       "- Auto-generated from ruff / pyright / eslint CLIs.",
                       "- Mirrors VS Code Problems panel — same engines under the hood.",
                       "- Re-run `python3 scripts/export_diagnostics.py` after edits to refresh.",
                       "- Claude: treat every line below as a real, current issue to fix.",
                       ""]

    out.append("## Python — ruff")
    out.extend(_ruff(root))
    out.append("")

    out.append("## Python — pyright (type errors)")
    out.extend(_pyright(root))
    out.append("")

    eslint_lines = _eslint(root)
    if eslint_lines:
        out.append("## Frontend — eslint")
        out.extend(eslint_lines)
        out.append("")

    content = "\n".join(out)
    (root / "DIAGNOSTICS.md").write_text(content, encoding="utf-8")
    print(f"WRITTEN: {root / 'DIAGNOSTICS.md'} ({len(content)} bytes)")


if __name__ == "__main__":
    main()
PYEOF

# ───────────────────────── .claude/CLAUDE.md directive append ────────────
DIRECTIVE_BLOCK='
## Context Loading Directive (appended)
- On session start, read `PROJECT_SUMMARY.md` at project root for full-project structural context — bullet-only, AST-derived, real file contracts.
- On any debugging / fix-my-code / review request, read `DIAGNOSTICS.md` at project root first — it mirrors the VS Code Problems panel exactly (ruff + pyright + eslint).
- Do NOT open individual source files to "check for issues" — DIAGNOSTICS.md already contains every current issue with exact file:line:col.
- Only open a source file once DIAGNOSTICS.md or PROJECT_SUMMARY.md points you to a specific line that needs editing.
- If DIAGNOSTICS.md is missing or stale (older than the last edit), say so and ask the user to run: `python3 scripts/export_diagnostics.py`
- If PROJECT_SUMMARY.md is missing, say so and ask the user to run: `python3 scripts/gen_project_summary.py . --force`
'

CLAUDE_MD="$ROOT/.claude/CLAUDE.md"
if [[ -f "$CLAUDE_MD" ]]; then
  if ! grep -q "Context Loading Directive (appended)" "$CLAUDE_MD"; then
    printf '%s' "$DIRECTIVE_BLOCK" >> "$CLAUDE_MD"
    echo "Appended context-loading directive to $CLAUDE_MD"
  else
    echo "Directive already present in $CLAUDE_MD — skipped"
  fi
else
  printf '# CLAUDE.md\n%s' "$DIRECTIVE_BLOCK" > "$CLAUDE_MD"
  echo "Created $CLAUDE_MD with directive"
fi

# ───────────────────────── generate PROJECT_SUMMARY.md now ────────────────
if [[ -f "$ROOT/scripts/gen_project_summary.py" ]]; then
  python3 "$ROOT/scripts/gen_project_summary.py" "$ROOT" || true
else
  echo "NOTE: scripts/gen_project_summary.py not found — run the project-summary"
  echo "      generator script separately, then re-run this script."
fi

echo ""
echo "Next steps on your machine:"
echo "  1. python3 scripts/export_diagnostics.py     # writes DIAGNOSTICS.md"
echo "  2. Open a new Claude Code session in $ROOT"
echo "  3. Ask Claude to fix issues — it will read DIAGNOSTICS.md, not your whole src/"
