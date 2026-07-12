#!/usr/bin/env bash
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 0
QUEUE=".claude/review-queue.log"
[ -s "$QUEUE" ] || exit 0
FILES=$(sort -u "$QUEUE")
: > "$QUEUE"
cat <<MSG >&2
CODE REVIEW REQUIRED — files modified this session:
$FILES

INSTRUCTION: Use the Agent tool with subagent_type "code-reviewer".
Pass exactly this file list as the prompt. Show ALL findings to the user
in this same turn, then continue. Do not ask the user whether to proceed —
run the review and report results as part of completing the task.
MSG
exit 2
