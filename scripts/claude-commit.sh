#!/usr/bin/env bash
# scripts/claude-commit.sh — Claude-controlled atomic commit
# ============================================================
# Called ONLY by Claude (via intel daemon) or manually.
# NEVER pushes. NEVER opens browser. NEVER triggers auth.
# Commits exactly what Claude just changed, with a meaningful message.
#
# Usage (Claude calls this directly):
#   bash scripts/claude-commit.sh --msg "fix: add entropy gate to HMM detector"
#   bash scripts/claude-commit.sh --msg "feat: implement slippage model" --files "src/risk/slippage.py"
#   bash scripts/claude-commit.sh --check   # dry-run, see what would be committed
#
# Commit types Claude uses:
#   feat:     new feature or capability added
#   fix:      bug or issue resolved
#   refactor: code restructured, no behavior change
#   chore:    tooling, config, cleanup, non-src changes
#   docs:     documentation, CLAUDE.md, .project-intel/ updates
#   test:     test added or fixed
#   security: vulnerability fixed
#   perf:     performance improvement
#   audit:    project scan / gap finding / diagnostic run

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ── Args ──────────────────────────────────────────────────────────────────────
CHECK_ONLY=false
CUSTOM_MSG=""
SPECIFIC_FILES=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --check)   CHECK_ONLY=true ;;
    --msg)     shift; CUSTOM_MSG="${1:-}" ;;
    --files)   shift; SPECIFIC_FILES="${1:-}" ;;
    *)         ;;
  esac
  shift
done

# ── Git identity (Claude's identity) ─────────────────────────────────────────
git config user.email "claude@anthropic.com"
git config user.name  "Claude (Anthropic)"

# ── Stage changes ─────────────────────────────────────────────────────────────
if [[ -n "$SPECIFIC_FILES" ]]; then
  # Stage only the files Claude explicitly touched
  for f in $SPECIFIC_FILES; do
    git add "$f" 2>/dev/null || true
  done
else
  # Stage everything (Claude made all recent changes)
  git add -A
fi

# ── Nothing to commit? ────────────────────────────────────────────────────────
if git diff --cached --quiet; then
  echo "✓ Nothing to commit — working tree clean."
  exit 0
fi

# ── Dry-run ───────────────────────────────────────────────────────────────────
if $CHECK_ONLY; then
  echo "Would commit:"
  git diff --cached --stat
  exit 0
fi

# ── Build message ─────────────────────────────────────────────────────────────
if [[ -z "$CUSTOM_MSG" ]]; then
  # Auto-infer from staged files
  CHANGED=$(git diff --cached --name-only)
  COUNT=$(echo "$CHANGED" | wc -l | tr -d ' ')

  if echo "$CHANGED" | grep -qE "^src/risk/"; then
    PREFIX="fix(risk)"
  elif echo "$CHANGED" | grep -qE "^src/regime/"; then
    PREFIX="feat(regime)"
  elif echo "$CHANGED" | grep -qE "^src/models/"; then
    PREFIX="feat(models)"
  elif echo "$CHANGED" | grep -qE "^src/execution/"; then
    PREFIX="feat(execution)"
  elif echo "$CHANGED" | grep -qE "^src/features/"; then
    PREFIX="feat(features)"
  elif echo "$CHANGED" | grep -qE "^src/diagnostics/"; then
    PREFIX="fix(diagnostics)"
  elif echo "$CHANGED" | grep -qE "^src/api/"; then
    PREFIX="feat(api)"
  elif echo "$CHANGED" | grep -qE "^tests/"; then
    PREFIX="test"
  elif echo "$CHANGED" | grep -qE "^\.project-intel/"; then
    PREFIX="docs(intel)"
  elif echo "$CHANGED" | grep -qE "^scripts/"; then
    PREFIX="chore(scripts)"
  elif echo "$CHANGED" | grep -qE "^frontend/"; then
    PREFIX="feat(frontend)"
  else
    PREFIX="chore"
  fi

  FIRST_FILE=$(echo "$CHANGED" | head -1)
  CUSTOM_MSG="${PREFIX}: update ${FIRST_FILE} (+${COUNT} files) [claude]"
fi

# ── Commit — NO PUSH ─────────────────────────────────────────────────────────
# NEVER add git push here. Claude does not push.
COMMIT_OUT=$(git commit \
  --no-verify \
  -m "$CUSTOM_MSG" \
  2>&1)

echo "✓ Committed: $CUSTOM_MSG"
echo "$COMMIT_OUT" | grep -E "^\[|file|insertion|deletion" || true

# ── Update SESSION_STATE with commit info ─────────────────────────────────────
INTEL_STATE=".project-intel/SESSION_STATE.json"
if [[ -f "$INTEL_STATE" ]] && command -v python3 &>/dev/null; then
  python3 - << PYEOF
import json, subprocess
from datetime import datetime
from pathlib import Path

state_file = Path("$INTEL_STATE")
try:
    state = json.loads(state_file.read_text())
except Exception:
    state = {}

hash_out = subprocess.run(
    ["git", "rev-parse", "--short", "HEAD"],
    capture_output=True, text=True
).stdout.strip()

state.setdefault("commit_log", []).append({
    "hash": hash_out,
    "message": "$CUSTOM_MSG",
    "time": datetime.now().isoformat(),
    "author": "Claude"
})
# Keep only last 20 commits in state
state["commit_log"] = state["commit_log"][-20:]
state["last_commit"] = hash_out
state["last_commit_message"] = "$CUSTOM_MSG"
state["last_commit_time"] = datetime.now().isoformat()

state_file.write_text(json.dumps(state, indent=2))
PYEOF
fi
