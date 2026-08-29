#!/usr/bin/env bash
# scripts/autocommit.sh — manual human commit helper (NO PUSH)
# ============================================================
# This script is for YOUR manual use from VSCode task or terminal.
# PUSH IS DISABLED — push manually when you are ready: git push
#
# Usage:
#   bash scripts/autocommit.sh                  # commit all staged
#   bash scripts/autocommit.sh --check          # dry-run
#   bash scripts/autocommit.sh --msg "message"  # custom message

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CHECK_ONLY=false
CUSTOM_MSG=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --check) CHECK_ONLY=true ;;
    --msg)   shift; CUSTOM_MSG="${1:-}" ;;
    *)       ;;
  esac
  shift
done

git config --get user.email >/dev/null || git config user.email "zulqarnain106612@gmail.com"
git config --get user.name  >/dev/null || git config user.name  "zulqarnain106612-cpu"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$BRANCH" == "main" ]]; then
  echo "Blocked: commit directly on 'main' is not allowed. Create a branch first." >&2
  exit 1
fi

git add -A

if git diff --cached --quiet; then
  echo "✓ Nothing to commit."
  exit 0
fi

if $CHECK_ONLY; then
  echo "Staged changes (dry-run):"
  git diff --cached --stat
  exit 0
fi

# ruff autofix before commit
if command -v ruff &>/dev/null; then
  ruff check src/ tests/ --fix --quiet
  ruff format src/ tests/ --quiet
  git add -A
fi

echo "Running gate: mypy + pytest (coverage >= 95%)..."
uv run mypy src/
uv run pytest tests/ -x -q --cov=src --cov-fail-under=95

if [[ -z "$CUSTOM_MSG" ]]; then
  CHANGED_FILES=$(git diff --cached --name-only | head -5 | tr '\n' ' ')
  CHANGED_COUNT=$(git diff --cached --name-only | wc -l | tr -d ' ')
  TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M UTC")
  if echo "$CHANGED_FILES" | grep -qE "test_|tests/"; then PREFIX="test"
  elif echo "$CHANGED_FILES" | grep -qE "src/"; then PREFIX="feat"
  else PREFIX="chore"
  fi
  CUSTOM_MSG="${PREFIX}: ${CHANGED_COUNT} file(s) — ${TIMESTAMP}"
fi

git commit -m "$CUSTOM_MSG"
echo "✓ Committed: $CUSTOM_MSG"
echo ""
echo "  Push when ready: git push origin $(git rev-parse --abbrev-ref HEAD)"
# ── NO PUSH — intentional ─────────────────────────────────────────────────────
