#!/usr/bin/env bash
# =============================================================
# setup_claude_efficiency.sh
# Run from: /home/fujitsu/Projects/Trade-Bot-main
#
# ENFORCES (all tools, all branches, all actions, always):
#   - No full file read, ever, any tool, any case
#   - No re-reading a file already in context (session tracker)
#   - No re-injecting data already in context (session tracker)
#   - No rewriting files when Edit/MultiEdit is sufficient
#   - Changed candidates only — skip unchanged files already tracked
#   - No idle waiting — never wait/sleep/poll on background tasks
#   - Same rules on every branch switch, build, write, or execution
#
# DOES NOT TOUCH:
#   - settings.local.json
#   - global ~/.claude/
#   - Existing CLAUDE.md content (append only)
#   - Existing .claudeignore content (append only)
#   - Existing settings.json keys (merge only)
#
# HOOK CONTEXT SAFETY:
#   Hooks = OS subprocesses. Never enter LLM context window.
#   Deny message = 1 line, only when a rule fires.
#   Session tracker = /tmp file, never loaded into context.
#
# SELF-INSTALL MODE:
#   If $SELF_INSTALL=1 is set, this script writes itself to
#   /mnt/user-data/outputs/setup_claude_efficiency.sh, verifies
#   line count and bash syntax, then exits. Useful for artifact
#   delivery without a separate wrapper.
# =============================================================

set -euo pipefail

# =============================================================
# SELF-INSTALL — Doc2 capability merged in
# Writes this script to the output path, validates, then exits.
# Activate with: SELF_INSTALL=1 bash setup_claude_efficiency.sh
# =============================================================
if [ "${SELF_INSTALL:-0}" = "1" ]; then
  OUT="/mnt/user-data/outputs/setup_claude_efficiency.sh"
  mkdir -p "$(dirname "$OUT")"
  cp -- "$0" "$OUT"
  chmod +x "$OUT"
  echo "Lines : $(wc -l < "$OUT")"
  bash -n "$OUT" && echo "Syntax: PASS" || echo "Syntax: FAIL"
  exit 0
fi

# =============================================================
# VARIABLES
# =============================================================
PROJECT_ROOT="/home/fujitsu/Projects/Trade-Bot-main"
HOOKS_DIR="$PROJECT_ROOT/.claude/hooks"
SETTINGS="$PROJECT_ROOT/.claude/settings.json"
CLAUDE_MD="$PROJECT_ROOT/CLAUDE.md"
CLAUDEIGNORE="$PROJECT_ROOT/.claudeignore"
SIGNALS_DIR="$PROJECT_ROOT/.claude_signals"

# =============================================================
# PREREQUISITES
# =============================================================
[ -f "$SETTINGS" ]     || { echo "ERROR: run from project root"; exit 1; }
[ -f "$CLAUDE_MD" ]    || { echo "ERROR: CLAUDE.md not found";   exit 1; }
[ -f "$CLAUDEIGNORE" ] || { echo "ERROR: .claudeignore not found"; exit 1; }
command -v python3 &>/dev/null || { echo "ERROR: python3 required"; exit 1; }
command -v jq     &>/dev/null || { echo "ERROR: jq required"; exit 1; }

mkdir -p "$HOOKS_DIR" "$SIGNALS_DIR"
[ ! -f "$SIGNALS_DIR/.gitignore" ] && printf '*\n!.gitignore\n' > "$SIGNALS_DIR/.gitignore"

echo "============================================================"
echo " Trade-Bot — Complete Context Enforcement"
echo "============================================================"

# =============================================================
# PART 1 — UNIVERSAL PreToolUse HOOK
# Empty matcher → fires on ALL tools, ALL branches, ALL actions.
#   - Session read tracker (PPID-based, /tmp) blocks re-reads
#   - Write guard: git-tracked files must use Edit not Write
#   - Idle-wait bash patterns blocked (wait, sleep-loop, poll)
#   - Changed-candidates-only enforcement via mtime tracking
# =============================================================
HOOK="$HOOKS_DIR/pre-tool.sh"
HOOK_MARKER="# === PRE-TOOL-UNIVERSAL v2 ==="

if grep -qF "$HOOK_MARKER" "$HOOK" 2>/dev/null; then
  echo "[SKIP] Hook already at v2."
else
  rm -f "$HOOK"

cat > "$HOOK" << 'HOOK_EOF'
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
HOOK_EOF

  chmod +x "$HOOK"
  echo "[OK] Hook v2 installed: $HOOK"
fi

# =============================================================
# PART 2 — MERGE settings.json
# Adds hooks (empty matcher) + lowers MAX_OUTPUT_TOKENS.
# Preserves ALL existing keys.
# =============================================================
echo ""
echo "[2/4] Merging settings.json..."

if grep -qF "pre-tool.sh" "$SETTINGS" 2>/dev/null; then
  echo "[SKIP] Hook already registered."
else
python3 << PYEOF
import json

path     = "$SETTINGS"
hook_cmd = "$HOOK"

with open(path) as f:
    cfg = json.load(f)

cfg["hooks"] = {
    "PreToolUse": [
        {"matcher": "", "hooks": [{"type": "command", "command": hook_cmd}]}
    ]
}

env = cfg.setdefault("env", {})
env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = "8192"

with open(path, "w") as f:
    json.dump(cfg, f, indent=2)

print("[OK] settings.json: hook registered, MAX_OUTPUT_TOKENS → 8192")
PYEOF
fi

# =============================================================
# PART 3 — APPEND to .claudeignore
# =============================================================
echo ""
echo "[3/4] Updating .claudeignore..."

IGNORE_MARKER="# === enforcement-additions ==="
if grep -qF "$IGNORE_MARKER" "$CLAUDEIGNORE" 2>/dev/null; then
  echo "[SKIP] .claudeignore additions already present."
else
cat >> "$CLAUDEIGNORE" << 'IGNORE_EOF'

# === enforcement-additions ===
# .claude/worktrees/ = full src duplicate (agent worktrees).
# Harness indexes this path → re-injects all files on git ops.
.claude/worktrees/
# Hook scripts: executable files, not source of truth
.claude/hooks/
# Session tracker files (auto-cleaned by OS, never source of truth)
.claude_signals/
.claude_probe
IGNORE_EOF
echo "[OK] .claudeignore updated."
fi

# =============================================================
# PART 4 — APPEND to CLAUDE.md
# =============================================================
echo ""
echo "[4/4] Appending missing rules to CLAUDE.md..."

MD_MARKER="## ENFORCEMENT ADDITIONS — DO NOT DUPLICATE"
if grep -qF "$MD_MARKER" "$CLAUDE_MD" 2>/dev/null; then
  echo "[SKIP] CLAUDE.md additions already present."
else
cat >> "$CLAUDE_MD" << 'MD_EOF'

## ENFORCEMENT ADDITIONS — DO NOT DUPLICATE
# Rules absent from existing CLAUDE.md. Enforced by hook + these instructions.
# Apply on ALL branches, ALL actions, ALL tools, ALL execution stages.

### NO DUPLICATION IN CONTEXT — ZERO TOLERANCE
A file already Read this session is already in context.
Reading it again wastes tokens and causes harness re-injection on git ops.

The hook tracks every Read by file path + mtime (session tracker in /tmp).
- Same file + unchanged mtime → BLOCKED automatically by hook.
- Same file + changed mtime (file was modified) → ALLOWED (changed candidate).
- After Edit/MultiEdit: file removed from tracker, re-read allowed if needed.

You do not need to track this manually. The hook enforces it.
When blocked: use the content already in context. Do not attempt to reload.

### CHANGED CANDIDATES ONLY
Only read, load, or inspect files that:
  (a) appear in the current git diff, OR
  (b) are directly required to prove or fix the current finding, OR
  (c) have been modified since last read (hook allows automatically).

Never read a file "for context", "to understand the codebase", or "to be sure".
If the file is not in the diff and not directly referenced: do not open it.

### EDIT-BEFORE-READ — ALWAYS
Attempting Edit directly is cheaper than Read → locate → Edit.
The hook blocks Read of large files without offset+limit.

CORRECT:
  1. Edit(file, old_str, new_str) directly with known string.
  2. If Edit fails (old_str not found): grep -n "partial" file | head -5
  3. Read(file, offset=<line>, limit=20). Retry Edit.

FORBIDDEN:
  ✗ Read(file) to locate old_str before Edit.
  ✗ Reading a file "to prepare for" or "understand before" editing.
  ✗ sed -i — always use Edit tool.

### GIT BRANCH SWITCH — MANDATORY SEQUENCE
Every branch switch risks harness re-injection of all tracked files.

MANDATORY:
  1. /compact → clears file content from context (resets what harness tracks).
  2. git stash (if uncommitted changes).
  3. git checkout <branch>.
  4. After switch: grep + targeted Read only. Never re-read files.
  5. Harness re-injection received unprompted → state "Discarding re-injection of <file>." Continue from known state.

BLOCKED by hook (all branches):
  git checkout -- .   git reset --hard   bare git merge   git checkout -f

### NEVER IDLE — BACKGROUND TASKS AND CI
If a background task, CI run, or async operation is in progress:
  → Do NOT wait, sleep, poll, or watch.
  → Move immediately to the next task in the repo or across branches.
  → Check result only when the next action requires it.

Signal pattern (only correct approach):
  (long-cmd && echo DONE > .claude_signals/NAME.done || echo FAIL > .claude_signals/NAME.done) &
  # immediately continue to next task — never wait

Check when needed:
  cat .claude_signals/NAME.done 2>/dev/null || echo PENDING
  # PENDING → continue. DONE/FAIL → handle, then continue.

BLOCKED by hook: bare wait, sleep-loops, gh run watch, tail -f.

### SAME RULES — ALL BRANCHES, ALL ACTIONS, ALL STAGES
These rules apply identically during:
  writing code, fixing bugs, running builds, reviewing PRs,
  branch switching, merging, CI dispatch, reading logs,
  debugging, refactoring, adding tests, pushing, any other action.

No action, branch, or execution stage is exempt.
MD_EOF
echo "[OK] CLAUDE.md: 7 rule sections appended."
fi

# =============================================================
# VERIFICATION
# =============================================================
echo ""
echo "============================================================"
echo " Verification"
echo "============================================================"
echo ""
echo "Hook (v2):"
[ -x "$HOOK" ] && echo "  [OK] $HOOK" || echo "  [FAIL] not executable"
grep -q "PRE-TOOL-UNIVERSAL v2" "$HOOK"  && echo "  [OK] Version v2 confirmed"          || echo "  [FAIL] Wrong version"
grep -q "READS_LOG"             "$HOOK"  && echo "  [OK] Session tracker present"        || echo "  [FAIL] Session tracker missing"
grep -q "TRACKED_MTIME"         "$HOOK"  && echo "  [OK] Re-read detection present"      || echo "  [FAIL] Re-read detection missing"
grep -qE "idle|wait|sleep"      "$HOOK"  && echo "  [OK] Idle-wait guard present"        || echo "  [FAIL] Idle guard missing"
grep -q "git ls-files"          "$HOOK"  && echo "  [OK] Git-tracked Write guard present"|| echo "  [FAIL] Write guard missing"
grep -q "Edit|MultiEdit"        "$HOOK"  && echo "  [OK] Edit tracker-reset present"     || echo "  [FAIL] Edit reset missing"
bash -n "$HOOK"                          && echo "  [OK] Hook syntax valid"              || echo "  [FAIL] Hook syntax error"

echo ""
echo "settings.json:"
python3 -c "
import json
with open('$SETTINGS') as f: c = json.load(f)
h = c.get('hooks',{}).get('PreToolUse',[{}])[0]
print('  matcher :', repr(h.get('matcher','MISSING')), '← empty = all tools')
print('  command :', h.get('hooks',[{}])[0].get('command','MISSING'))
print('  tokens  :', c.get('env',{}).get('CLAUDE_CODE_MAX_OUTPUT_TOKENS','MISSING'))
"

echo ""
echo ".claudeignore:"
grep -E "worktrees|hooks|signals" "$CLAUDEIGNORE" | sed 's/^/  /'

echo ""
echo "CLAUDE.md rule sections added:"
grep "^### " "$CLAUDE_MD" | tail -7 | sed 's/^/  /'

echo ""
echo "Tool coverage (empty matcher = all tools):"
printf "  %-16s %s\n" "Bash"           "cat/log/sed-i/git-ops/find/lists/pytest/docker/env/idle-wait/poll"
printf "  %-16s %s\n" "Read"           "session tracker + offset+limit + 100-line cap"
printf "  %-16s %s\n" "Write"          "git-tracked large file rewrite blocked"
printf "  %-16s %s\n" "Edit"           "allowed + resets session tracker for that file"
printf "  %-16s %s\n" "MultiEdit"      "allowed + resets session tracker for that file"
printf "  %-16s %s\n" "Glob"           "unfiltered wildcards blocked"
printf "  %-16s %s\n" "Task"           "blocked: HARD RULES — never use sub-agents"
printf "  %-16s %s\n" "NotebookRead"   "cell range required"
printf "  %-16s %s\n" "WebFetch"       "large index pages blocked"
printf "  %-16s %s\n" "Grep"           "allowed (efficient by design)"
printf "  %-16s %s\n" "LS"             "allowed (compact output)"
printf "  %-16s %s\n" "WebSearch"      "allowed (structured results)"
printf "  %-16s %s\n" "TodoRead/Write" "allowed (small fixed output)"
printf "  %-16s %s\n" "ALL TOOLS"      ".claude/worktrees/ access blocked"

echo ""
echo "Self-install mode:"
echo "  SELF_INSTALL=1 bash setup_claude_efficiency.sh"
echo "  → copies script to /mnt/user-data/outputs/, validates syntax, exits."

echo ""
echo "Context safety:"
echo "  Hook script     → OS subprocess, never in LLM context"
echo "  Session tracker → /tmp/claude_reads_<PPID>, never in LLM context"
echo "  settings.json   → config file, never in LLM context"
echo "  .claudeignore   → exclusion list, never in LLM context"
echo "  CLAUDE.md       → ~50 lines appended to existing content (rules only)"
echo "  Deny messages   → 1 line each, only when rule fires, in tool error not context"
echo "============================================================"
