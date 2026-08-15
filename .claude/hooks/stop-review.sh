#!/usr/bin/env bash
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 0
QUEUE=".claude/review-queue.log"
[ -s "$QUEUE" ] || exit 0
FILES=$(sort -u "$QUEUE")
: > "$QUEUE"
# Per CLAUDE.md Hard Rules: no local test/lint/review — code review runs in
# GitHub Actions (claude-code-review.yml) against the pushed branch/PR, not
# via a local code-reviewer subagent invocation. Informational only (exit 0,
# does not block Stop) — push and check `gh pr checks` instead.
cat <<MSG
NOTE: files modified this session (review happens in CI, not locally):
$FILES

Push this branch / open or update the PR and let claude-code-review.yml
run the review. Check with: gh pr checks <PR#>
MSG
exit 0
