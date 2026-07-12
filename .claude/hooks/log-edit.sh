#!/usr/bin/env bash
set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)" || exit 0
QUEUE=".claude/review-queue.log"
INPUT=$(cat)

TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null) || exit 0
if [ "$TOOL" = "Bash" ]; then
  CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null) || exit 0
  [ -n "$CMD" ] || exit 0
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
