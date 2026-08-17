#!/usr/bin/env bash
# === PRE-TOOL-UNIVERSAL v2 ===
# Fires on ALL tools via empty matcher in .claude/settings.json.
# OS subprocess — never enters LLM context window.
# Deny message = 1 line. Only emitted when a rule fires.
#
# SESSION TRACKER:
#   /tmp/claude_reads_<PPID> — tracks file:mtime for every Read this session.
#   PPID is stable within a Claude Code session (all hooks share same parent).
#   Re-read of same file with same mtime → BLOCKED (already in context).
#   Re-read after file modified (mtime changed) → ALLOWED (changed candidate).
#   Tracker is /tmp — auto-cleaned by OS, never enters context.

INPUT=$(cat)
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)
[ -z "$TOOL" ] && exit 0

READS_LOG="/tmp/claude_reads_${PPID}"
WORKTREE=".claude/worktrees"

deny() {
  printf '{"decision":"block","reason":"[%s] %s"}\n' "$TOOL" "$1"
  exit 0
}

# =============================================================
# WORKTREE GUARD — ALL TOOLS
# .claude/worktrees/ = full src duplicate. Access = re-injection.
# =============================================================
ALL_STRINGS=$(echo "$INPUT" | jq -r '.. | strings' 2>/dev/null | tr '\n' ' ')
echo "$ALL_STRINGS" | grep -qF "$WORKTREE" &&
  deny "Access to .claude/worktrees/ blocked. Full src duplicate — triggers harness re-injection of entire tree."

# =============================================================
# TOOL RULES
# =============================================================
case "$TOOL" in

# -------------------------------------------------------------
# BASH
# -------------------------------------------------------------
Bash)
  CMD=$(echo "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
  [ -z "$CMD" ] && exit 0

  # ── Full file reads ──────────────────────────────────────
  echo "$CMD" | grep -qE '^\s*cat\s+[^|>&;]+$' &&
    deny "cat blocked. grep -n <target> <file> | head -5 → Read(file,offset=N,limit=50)."

  echo "$CMD" | grep -qE '\bcat\b[^|>]*\.(log|out)[[:space:]]*$' &&
    deny "Log cat blocked. grep -m20 'ERROR|FAIL|Exception' file.log | tail -20"

  # ── sed -i: use Edit tool ────────────────────────────────
  echo "$CMD" | grep -qE '\bsed\b[^|]*-i' &&
    deny "sed -i blocked. Use Edit(old_str,new_str) directly — no Read required."

  # ── Git re-injection triggers ────────────────────────────
  echo "$CMD" | grep -qE 'git\s+checkout\s+.*--\s+\.' &&
    deny "git checkout -- . blocked. Clobbers working tree → full harness re-injection. Use: git checkout -- specific/file"

  echo "$CMD" | grep -qE 'git\s+reset\s+--hard' &&
    deny "git reset --hard blocked. Triggers harness re-injection of all tracked files. Requires explicit user auth."

  echo "$CMD" | grep -qE '\bgit\s+merge\b' &&
  ! echo "$CMD" | grep -qE '\-\-no-commit|\-\-abort|\-\-continue|\-\-stat|\-\-ff-only|\-\-squash' &&
    deny "Bare git merge blocked. Use: git merge --no-commit --no-ff <branch>"

  echo "$CMD" | grep -qE 'git\s+checkout\s+-[fmB]' &&
    deny "git checkout force flag blocked. /compact first, then switch branch."

  # ── Uncapped git output ──────────────────────────────────
  echo "$CMD" | grep -qE '^\s*git\s+log\b' &&
  ! echo "$CMD" | grep -qE '\-\-oneline|-n\s*[0-9]+|-[0-9]+|\-\-format|\-\-pretty' &&
    deny "Uncapped git log. Use: git log --oneline -10"

  echo "$CMD" | grep -qE '^\s*git\s+diff\b' &&
  ! echo "$CMD" | grep -qE '\-\-stat|HEAD~[0-9]|[a-f0-9]{6,}|--\s+\S|\-\-name-only|\-\-cached' &&
    deny "Bare git diff. Use: git diff --stat then git diff -- path/to/file"

  # ── Unbounded filesystem ops ─────────────────────────────
  echo "$CMD" | grep -qE '\bfind\b[^|]*-type\s+f\s*$' &&
    deny "Unbounded find. Add: -maxdepth 3 -name '*.py'"

  echo "$CMD" | grep -qE '^\s*(ls\s+-[lRrta]+\s*$|ls\s+\.\s*$)' &&
    deny "Broad ls blocked. Use: ls src/ or LS tool."

  # ── Lock / large dependency files ────────────────────────
  echo "$CMD" | grep -qE '\b(cat|head|tail)\b[^|]*(requirements\.lock|package-lock\.json|yarn\.lock|poetry\.lock)' &&
    deny "Lock file read blocked. Use: pip show <pkg> or grep from pyproject.toml"

  # ── Uncapped output commands ─────────────────────────────
  echo "$CMD" | grep -qE '^\s*(pip list|pip freeze|npm list)\s*$' &&
    deny "Uncapped list. Use: pip list | head -20 or pip show <pkg>"

  echo "$CMD" | grep -qE '^\s*(pytest|uv run pytest)\s*[^|>2]*$' &&
  ! echo "$CMD" | grep -qE '2>&1\s*\||>\s*\S' &&
    deny "Uncapped pytest. Use: uv run pytest 2>&1 | tail -30"

  echo "$CMD" | grep -qE '^\s*docker\s+build\b' &&
  ! echo "$CMD" | grep -qE '2>&1\s*\|' &&
    deny "Uncapped docker build. Use: docker build . 2>&1 | tail -20"

  echo "$CMD" | grep -qE '^\s*ps\s+(aux|-aux|ef)\s*$' &&
    deny "Uncapped ps. Use: ps aux | grep <process> | head -10"

  echo "$CMD" | grep -qE '^\s*(env|printenv)\s*$' &&
    deny "Full env dump blocked. Use: echo \$VAR_NAME"

  # ── .env access ──────────────────────────────────────────
  echo "$CMD" | grep -qE '[^a-z\-]\.env\b' &&
  ! echo "$CMD" | grep -qE '\.env\.example|\.env\.sample|\.env\.template' &&
    deny ".env access blocked per HARD RULES. Use .env.example only."

  # ── IDLE WAIT — never block on background tasks ──────────
  echo "$CMD" | grep -qE '^\s*wait\s*$' &&
    deny "Bare 'wait' blocked. Never idle-wait on background jobs. Check .claude_signals/<name>.done then continue next task."

  echo "$CMD" | grep -qE 'while.*sleep|sleep.*while|for.*sleep|sleep [0-9]+.*wait' &&
    deny "Sleep-loop/polling blocked. Use signal pattern: check .claude_signals/<name>.done; if PENDING continue next task."

  echo "$CMD" | grep -qE 'gh run watch|gh run view.*--watch|watch.*gh run' &&
    deny "CI watch/poll blocked. Dispatch CI then immediately move to next task. Check results only when needed: gh run view <id> | head -20"

  echo "$CMD" | grep -qE 'tail\s+-f\b|journalctl.*-f\b' &&
    deny "tail -f / follow-mode blocked. Use: tail -n 40 or grep -m20 for targeted log reads."

  ;;

# -------------------------------------------------------------
# READ — session tracker + offset+limit enforcement
# -------------------------------------------------------------
Read)
  FILE=$(echo "$INPUT"   | jq -r '.tool_input.file_path // empty' 2>/dev/null)
  OFFSET=$(echo "$INPUT" | jq -r '.tool_input.offset    // empty' 2>/dev/null)
  LIMIT=$(echo "$INPUT"  | jq -r '.tool_input.limit     // empty' 2>/dev/null)

  [ -z "$FILE" ] && exit 0
  [ ! -f "$FILE" ] && exit 0

  FILE_MTIME=$(stat -c %Y "$FILE" 2>/dev/null || stat -f %m "$FILE" 2>/dev/null || echo 0)
  LINES=$(wc -l < "$FILE" 2>/dev/null || echo 0)

  # ── Session tracker: block re-reads of unchanged files ───
  if [ -f "$READS_LOG" ]; then
    TRACKED=$(grep "^${FILE}:" "$READS_LOG" 2>/dev/null || true)
    if [ -n "$TRACKED" ]; then
      TRACKED_MTIME=$(echo "$TRACKED" | cut -d: -f2)
      if [ "$TRACKED_MTIME" = "$FILE_MTIME" ]; then
        deny "Read($FILE) blocked: already in context (file unchanged since last read, mtime=$FILE_MTIME). Use content already available. File will be re-allowed automatically if modified."
      fi
      grep -v "^${FILE}:" "$READS_LOG" > "${READS_LOG}.tmp" 2>/dev/null && mv "${READS_LOG}.tmp" "$READS_LOG" || true
    fi
  fi

  # ── Small file (<30 lines): allow full read ───────────────
  if [ "$LINES" -lt 30 ]; then
    echo "${FILE}:${FILE_MTIME}" >> "$READS_LOG"
    exit 0
  fi

  # ── Large file: offset+limit required ────────────────────
  { [ -z "$OFFSET" ] || [ -z "$LIMIT" ]; } &&
    deny "Read($FILE, $LINES lines) without offset+limit blocked. Steps: (1) grep -n <target> $FILE | head -5  (2) Read(file,offset=N,limit=50). For Edit: attempt Edit(old_str,new_str) directly — no Read needed if string is known."

  # ── Limit cap: max 100 lines per Read call ────────────────
  { echo "$LIMIT" | grep -qE '^[0-9]+$'; } && [ "$LIMIT" -gt 100 ] &&
    deny "Read(limit=$LIMIT) exceeds 100-line cap. Use targeted chunks of limit=50."

  echo "${FILE}:${FILE_MTIME}" >> "$READS_LOG"
  exit 0
  ;;

# -------------------------------------------------------------
# WRITE — block rewrites of git-tracked files
# -------------------------------------------------------------
Write)
  FILE=$(echo "$INPUT"    | jq -r '.tool_input.file_path // empty' 2>/dev/null)
  CONTENT=$(echo "$INPUT" | jq -r '.tool_input.content   // empty' 2>/dev/null)

  [ -z "$FILE" ] && exit 0

  if ! git ls-files --error-unmatch "$FILE" &>/dev/null 2>&1; then
    exit 0
  fi

  OLD_LINES=$(wc -l < "$FILE" 2>/dev/null || echo 0)
  NEW_LINES=$(printf '%s' "$CONTENT" | wc -l 2>/dev/null || echo 0)

  [ "$OLD_LINES" -lt 50 ] && exit 0

  [ "$NEW_LINES" -gt 80 ] &&
    deny "Write($FILE): rewriting $OLD_LINES-line git-tracked file blocked. Use Edit(old_str,new_str) or MultiEdit for targeted changes only. Full rewrite requires explicit user instruction."

  exit 0
  ;;

# -------------------------------------------------------------
# EDIT / MULTIEDIT — reset session tracker for modified file
# -------------------------------------------------------------
Edit|MultiEdit)
  FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null)
  [ -n "$FILE" ] && [ -f "$READS_LOG" ] &&
    grep -v "^${FILE}:" "$READS_LOG" > "${READS_LOG}.tmp" 2>/dev/null &&
    mv "${READS_LOG}.tmp" "$READS_LOG" 2>/dev/null || true
  exit 0
  ;;

# -------------------------------------------------------------
# GLOB — block unfiltered wildcards
# -------------------------------------------------------------
Glob)
  PATTERN=$(echo "$INPUT" | jq -r '.tool_input.pattern // empty' 2>/dev/null)
  [ -z "$PATTERN" ] && exit 0
  echo "$PATTERN" | grep -qE '^\*\*\/\*$|^\*\/\*$|^\*$|^\.\*$' &&
    deny "Glob('$PATTERN') returns entire project tree. Add extension: **/*.py, src/**/*.py"
  exit 0
  ;;

# -------------------------------------------------------------
# TASK — blocked per project HARD RULES
# -------------------------------------------------------------
Task)
  deny "Task (sub-agent) blocked per HARD RULES: 'Never use Agent/sub-agents.'"
  ;;

# -------------------------------------------------------------
# NOTEBOOKREAD — cell range required
# -------------------------------------------------------------
NotebookRead)
  FILE=$(echo "$INPUT"  | jq -r '.tool_input.notebook_path // empty' 2>/dev/null)
  START=$(echo "$INPUT" | jq -r '.tool_input.cell_start    // empty' 2>/dev/null)
  END=$(echo "$INPUT"   | jq -r '.tool_input.cell_end      // empty' 2>/dev/null)
  [ -z "$FILE" ] && exit 0
  { [ -z "$START" ] || [ -z "$END" ]; } &&
    deny "NotebookRead($FILE) without cell range blocked. grep for cell content to identify range first."
  exit 0
  ;;

# -------------------------------------------------------------
# WEBFETCH — block large index pages
# -------------------------------------------------------------
WebFetch)
  URL=$(echo "$INPUT" | jq -r '.tool_input.url // empty' 2>/dev/null)
  [ -z "$URL" ] && exit 0
  echo "$URL" | grep -qE '(github\.com/[^/]+/[^/]+/?$|/wiki/?$|/docs/?$|/blob/main/README)' &&
    deny "WebFetch($URL) is a large index page. WebSearch for the specific section URL first."
  exit 0
  ;;

# -------------------------------------------------------------
# ALLOWED (efficient, no guard needed):
#   Grep, LS, WebSearch, TodoRead, TodoWrite
# -------------------------------------------------------------
Grep|LS|WebSearch|TodoRead|TodoWrite)
  exit 0
  ;;

esac

exit 0
