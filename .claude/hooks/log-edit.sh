#!/usr/bin/env bash
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 0
QUEUE=".claude/review-queue.log"
INPUT=$(cat)

TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null) || exit 0
if [ "$TOOL" = "Bash" ]; then
  CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null) || exit 0
  [ -n "$CMD" ] || exit 0
  # Only queue paths when the command actually looks write-capable -- a bare
  # path mention (grep/cat/sed -n/head/ls/wc/find on a file) is a read, not
  # an edit, and previously got queued just as eagerly as a real write.
  printf '%s\n' "$CMD" | grep -qE '(^|[^0-9])>>?[^=]|sed[[:space:]]+-i|\bcp\b|\bmv\b|\btee\b|apply_patch|\btouch\b|\brm\b' \
    || exit 0
  # Extract candidate file targets from common write-capable commands
  printf '%s\n' "$CMD" | grep -oE '([[:alnum:]_./-]*/(src|tests|scripts)/[[:alnum:]_./-]+)' \
    | sort -u | while read -r f; do echo "$f" >> "$QUEUE"; done
  exit 0
fi

FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null) || exit 0
[ -n "$FILE_PATH" ] || exit 0
case "$FILE_PATH" in
  */src/*|*/tests/*|*/scripts/*) echo "$FILE_PATH" >> "$QUEUE" ;;
esac
exit 0
