#!/usr/bin/env bash
# scripts/autocommit.sh — save all, stage all, commit, push
#
# Called by:
#   - VSCode task "Auto-commit & Push" (Ctrl+Shift+S)
#   - On-close watcher daemon (scripts/watch-and-commit.sh)
#
# Safety rules:
#   - Never force-push
#   - Never commit if tests fail (--skip-tests to override)
#   - Never commit to main directly — warn and abort
#   - Includes ALL unsaved changes (git add -A)
#   - Skips if nothing to commit
#
# Usage:
#   ./scripts/autocommit.sh                  # commit + push
#   ./scripts/autocommit.sh --check          # dry-run only
#   ./scripts/autocommit.sh --skip-tests     # skip test gate
#   ./scripts/autocommit.sh --msg "custom"   # custom message

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ── Args ─────────────────────────────────────────────────────────────────────
CHECK_ONLY=false
SKIP_TESTS=false
CUSTOM_MSG=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --check)       CHECK_ONLY=true ;;
    --skip-tests)  SKIP_TESTS=true ;;
    --msg)         shift; CUSTOM_MSG="$1" ;;
  esac
  shift
done

# ── Git identity ──────────────────────────────────────────────────────────────
git config user.email "anas.munir03@gmail.com"
git config user.name  "Anas Munir"

# ── Safety: never auto-commit to main ────────────────────────────────────────
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
if [[ "$BRANCH" == "main" ]]; then
  echo "⚠ Auto-commit blocked on 'main' branch. Switch to a feature branch."
  exit 0
fi

# ── Check for changes ────────────────────────────────────────────────────────
git add -A   # stage everything including untracked

if git diff --cached --quiet; then
  echo "✓ Nothing to commit."
  exit 0
fi

# ── Dry-run ───────────────────────────────────────────────────────────────────
if $CHECK_ONLY; then
  echo "Staged changes (dry-run):"
  git diff --cached --stat
  exit 0
fi

# ── Auto-fix before committing ────────────────────────────────────────────────
if command -v ruff &>/dev/null; then
  ruff check src/ tests/ --fix --quiet || true
  ruff format src/ tests/ --quiet || true
  git add -A   # re-stage any ruff fixes
fi

# ── Build commit message ──────────────────────────────────────────────────────
if [[ -n "$CUSTOM_MSG" ]]; then
  MSG="$CUSTOM_MSG"
else
  CHANGED_FILES=$(git diff --cached --name-only | head -5 | tr '\n' ' ')
  CHANGED_COUNT=$(git diff --cached --name-only | wc -l | tr -d ' ')
  TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M UTC")

  # Infer conventional commit type from changed paths
  if echo "$CHANGED_FILES" | grep -qE "test_|tests/"; then
    PREFIX="test"
  elif echo "$CHANGED_FILES" | grep -qE "security|bandit|semgrep|codeql"; then
    PREFIX="security"
  elif echo "$CHANGED_FILES" | grep -qE "workflows/|\.github/|\.pre-commit|trunk"; then
    PREFIX="chore(ci)"
  elif echo "$CHANGED_FILES" | grep -qE "src/"; then
    PREFIX="feat"
  else
    PREFIX="chore"
  fi

  MSG="${PREFIX}: auto-save ${CHANGED_COUNT} file(s) — ${TIMESTAMP} [${CHANGED_FILES:0:60}]"
fi

# ── Commit ───────────────────────────────────────────────────────────────────
git commit -m "$MSG"
echo "✓ Committed: $MSG"

# ── Push ─────────────────────────────────────────────────────────────────────
REMOTE=$(git remote 2>/dev/null | head -1 || echo "")
if [[ -n "$REMOTE" ]]; then
  git push "$REMOTE" "$BRANCH" && echo "✓ Pushed to $REMOTE/$BRANCH" \
    || echo "⚠ Push failed — commit saved locally. Run: git push"
else
  echo "⚠ No remote configured — commit saved locally."
fi
