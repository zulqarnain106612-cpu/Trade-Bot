# Claude Code — Enforced Action Patterns
# Every action Claude might take, after hook deployment.
# Format: BLOCKED (what hook stops) → ENFORCED (what Claude must do instead)
# ============================================================

## 1. READ A FILE

### Find a function definition
BLOCKED:   Read("src/engine/orchestrator.py")                    # 400-line file, no bounds
ENFORCED:  Bash("grep -n 'def run_cycle' src/engine/orchestrator.py | head -5")
           Read("src/engine/orchestrator.py", offset=42, limit=30)

### Read a config value
BLOCKED:   Read("src/config.py")                                 # entire config file
ENFORCED:  Bash("grep -n 'MAX_POSITION_SIZE\|KELLY_FRACTION' src/config.py | head -5")
           Read("src/config.py", offset=N, limit=10)

### Read a class definition
BLOCKED:   Read("src/risk/kelly.py")
ENFORCED:  Bash("grep -n 'class Kelly\|def compute' src/risk/kelly.py | head -10")
           Read("src/risk/kelly.py", offset=N, limit=50)

### Read imports of a module
BLOCKED:   Read("src/execution/order_manager.py")
ENFORCED:  Bash("grep -n '^from\|^import' src/execution/order_manager.py | head -20")

### Read a small file (<30 lines)
ALLOWED:   Read("src/__init__.py")                               # <30 lines: hook passes it through

---

## 2. FIX CODE — SINGLE LINE OR BLOCK

### Fix a known value (old_str known)
BLOCKED:   Read("src/risk/kelly.py")  →  find line  →  Edit(...)   # Read is BLOCKED
ENFORCED:  Edit("src/risk/kelly.py",
             old_str="kelly_fraction = position_size",
             new_str="kelly_fraction = min(position_size, MAX_KELLY_CAP)")

### Fix when exact string is unknown
BLOCKED:   Read("src/risk/kelly.py")                             # hook blocks it
ENFORCED:  Bash("grep -n 'kelly_fraction' src/risk/kelly.py | head -5")
           # returns: line 87: kelly_fraction = position_size
           Read("src/risk/kelly.py", offset=83, limit=15)
           Edit("src/risk/kelly.py",
             old_str="kelly_fraction = position_size",
             new_str="kelly_fraction = min(position_size, MAX_KELLY_CAP)")

### Fix multiple locations in one file
ENFORCED:  MultiEdit("src/risk/kelly.py", [
             {old_str: "...", new_str: "..."},
             {old_str: "...", new_str: "..."}
           ])

### Fix across multiple files
ENFORCED:  Bash("grep -rn 'wrong_symbol' src/ --include='*.py' | head -10")
           # identify all files, then one Edit per file

### Add a new method to a class
BLOCKED:   Read(file) → understand structure → Write entire file  # both blocked
ENFORCED:  Bash("grep -n 'class RiskManager\|def ' src/risk/kelly.py | head -15")
           Read("src/risk/kelly.py", offset=<class_end_line - 5>, limit=10)
           Edit("src/risk/kelly.py",
             old_str="    # end of class",
             new_str="    # end of class\n\n    def new_method(self):\n        ...")

---

## 3. GET LOGS — ERRORS, FAILURES, TRACES

### Get recent errors
BLOCKED:   Bash("cat logs/trading.log")                          # hook blocks full log cat
ENFORCED:  Bash("grep -m20 'ERROR\|FAIL\|Exception' logs/trading.log | tail -20")

### Get a traceback
BLOCKED:   Bash("tail -n 200 logs/trading.log")                  # too many lines
ENFORCED:  Bash("grep -n 'Traceback\|Error' logs/trading.log | tail -5")
           Bash("grep -A15 'Traceback' logs/trading.log | tail -30")

### Get last N log lines
ENFORCED:  Bash("tail -n 40 logs/trading.log")

### Search logs for specific event
ENFORCED:  Bash("grep -n 'OrderFill\|order_id=12345' logs/trading.log | tail -10")

### Get logs from a time range
ENFORCED:  Bash("grep '2024-01-15 14:' logs/trading.log | grep 'ERROR\|WARN' | head -20")

### Get test failure output
BLOCKED:   Bash("uv run pytest")                                 # hook blocks uncapped
ENFORCED:  Bash("uv run pytest 2>&1 | tail -30")
           Bash("uv run pytest tests/test_kelly.py -v 2>&1 | tail -30")
           Bash("uv run pytest 2>&1 | grep -E 'FAIL|ERROR|assert' | head -20")

---

## 4. PUSH CODE — GIT WORKFLOW

### Check what changed
BLOCKED:   Bash("git diff")                                      # bare diff, hook blocks
ENFORCED:  Bash("git diff --stat")
           Bash("git diff -- src/risk/kelly.py")                 # specific file

### Stage and commit
ENFORCED:  Bash("git add src/risk/kelly.py")
           Bash("git add -p")                                    # interactive, allowed
           Bash("git commit -m 'fix: cap kelly fraction at MAX_KELLY_CAP'")

### Push
ENFORCED:  Bash("git push origin <branch-name>")
           Bash("git push --set-upstream origin <branch-name>")

### Check commit history
BLOCKED:   Bash("git log")                                       # uncapped, hook blocks
ENFORCED:  Bash("git log --oneline -10")
           Bash("git log --oneline -5 -- src/risk/kelly.py")

### Check current status
ENFORCED:  Bash("git status --short")
           Bash("git branch --show-current")

### Switch branch (safe pattern)
BLOCKED:   Bash("git checkout feature-branch")                   # without /compact first
ENFORCED:  # Step 1: /compact  (clear file content from context)
           # Step 2: Bash("git stash") if uncommitted changes
           # Step 3: Bash("git checkout feature-branch")
           # Step 4: work from grep/targeted reads only — do NOT re-read files

### Merge branch (safe pattern)
BLOCKED:   Bash("git merge feature-branch")                      # hook blocks bare merge
ENFORCED:  Bash("git merge --no-commit --no-ff feature-branch")
           Bash("git diff --stat HEAD")                          # inspect before commit
           Bash("git commit -m 'merge: ...'")                    # if satisfied

### Trigger CI
ENFORCED:  Bash("gh workflow run ci.yml --ref <branch>")
           Bash("gh run view --branch <branch> | head -20")
           Bash("gh run list --branch <branch> --limit 3")

---

## 5. SEARCH CODEBASE

### Find where a symbol is defined
ENFORCED:  Bash("grep -rn 'def compute_kelly\|class Kelly' src/ --include='*.py' | head -10")
           Grep("def compute_kelly", "src/")

### Find all usages of a function
ENFORCED:  Bash("grep -rn 'compute_kelly(' src/ --include='*.py' | head -15")

### Find a file by name
ENFORCED:  Bash("find . -name 'kelly.py' -maxdepth 5")
           Bash("find src/ -name '*.py' -maxdepth 3 | head -20")

### Find all Python files in a directory
ENFORCED:  Glob("src/risk/**/*.py")
           Bash("find src/risk/ -name '*.py' -maxdepth 2")

### Find config keys
ENFORCED:  Bash("grep -rn 'MAX_POSITION\|KELLY' src/config.py | head -10")

### Find all imports of a module
ENFORCED:  Bash("grep -rn 'from src.risk.kelly\|import kelly' src/ --include='*.py' | head -10")

---

## 6. UNDERSTAND PROJECT STRUCTURE

### See top-level layout
BLOCKED:   Bash("find . -type f")                                # hook blocks unbounded find
BLOCKED:   Bash("ls -R")                                         # hook blocks recursive ls
ENFORCED:  LS(".")                                               # compact top-level
           LS("src/")
           Bash("find src/ -name '*.py' -maxdepth 2 | head -20")

### See what a module exports
ENFORCED:  Bash("grep -n '^class\|^def\|^__all__' src/risk/kelly.py | head -20")

### See entry points
ENFORCED:  Bash("grep -rn 'if __name__\|def main' src/ --include='*.py' | head -10")

### See test structure
ENFORCED:  Bash("find tests/ -name 'test_*.py' -maxdepth 3 | head -20")
           Bash("grep -rn 'def test_' tests/test_kelly.py | head -15")

---

## 7. DEPENDENCIES

### Check if package is installed
BLOCKED:   Bash("pip list")                                      # hook blocks uncapped
BLOCKED:   Bash("pip freeze")                                    # hook blocks
ENFORCED:  Bash("pip show xgboost | head -5")
           Bash("pip show ccxt | grep 'Version\|Location'")

### Check project dependencies
ENFORCED:  Bash("cat pyproject.toml | head -40")                 # pyproject.toml is small
           Bash("grep 'ccxt\|xgboost\|fastapi' pyproject.toml")

### Install a package
ENFORCED:  Bash("uv pip install <package>")                      # pre-approved in settings.local

---

## 8. DEBUGGING A FAILURE

### Test fails — get the failure
BLOCKED:   Bash("uv run pytest")                                 # hook blocks uncapped
ENFORCED:  Bash("uv run pytest tests/test_kelly.py 2>&1 | tail -30")
           Bash("uv run pytest -x 2>&1 | tail -30")             # stop on first failure

### Isolate the failing test
ENFORCED:  Bash("uv run pytest tests/test_kelly.py::test_cap_enforcement -v 2>&1 | tail -20")

### Find the failing line in source
ENFORCED:  Bash("grep -n 'cap_enforcement\|kelly_fraction' src/risk/kelly.py | head -10")
           Read("src/risk/kelly.py", offset=N, limit=20)

### Check a specific assertion failure
ENFORCED:  Bash("uv run pytest -x 2>&1 | grep -A10 'AssertionError\|assert ' | head -20")

### Check runtime error in live log
ENFORCED:  Bash("grep -m10 'RuntimeError\|ValueError\|KeyError' logs/trading.log | tail -10")
           Bash("grep -B2 -A10 'RuntimeError' logs/trading.log | tail -30")

---

## 9. READING CONFIG / ENVIRONMENT

### Get a specific config value
BLOCKED:   Read("src/config.py")                                 # hook blocks (>30 lines)
ENFORCED:  Bash("grep -n 'DATABASE_URL\|API_KEY_NAME\|MAX_' src/config.py | head -10")

### Read .env.example (not .env)
BLOCKED:   Bash("cat .env")                                      # hook blocks .env access
ENFORCED:  Bash("cat .env.example | head -20")                   # example only

### Check environment variable at runtime
ENFORCED:  Bash("echo $SPECIFIC_VAR_NAME")
           Bash("grep 'VAR_NAME' .env.example")

---

## 10. DOCKER / SERVICES

### Build image
BLOCKED:   Bash("docker build .")                                # hook blocks uncapped
ENFORCED:  Bash("docker build . 2>&1 | tail -20")

### Check running containers
ENFORCED:  Bash("docker ps --format 'table {{.Names}}\t{{.Status}}' | head -10")

### Get container logs
BLOCKED:   Bash("docker logs trade-bot")                         # uncapped
ENFORCED:  Bash("docker logs trade-bot --tail 30")
           Bash("docker logs trade-bot 2>&1 | grep 'ERROR\|WARN' | tail -20")

---

## 11. CI — READ RESULTS BEFORE DISPATCH

### Check existing CI run
ENFORCED:  Bash("gh run list --branch $(git branch --show-current) --limit 3")
           Bash("gh run view <run-id> | head -30")

### Get CI failure detail
ENFORCED:  Bash("gh run view <run-id> --log-failed | head -50")
           Bash("gh run view <run-id> --log-failed | grep -A10 'FAIL\|Error' | head -30")

### Dispatch CI only when content changed
ENFORCED:  Bash("gh workflow run ci.yml --ref $(git branch --show-current)")
           # Never re-dispatch if tree SHA unchanged

---

## 12. WEB RESEARCH

### Find API documentation
BLOCKED:   WebFetch("https://docs.ccxt.com")                     # hook blocks large index
ENFORCED:  WebSearch("ccxt create_order params python")
           WebFetch("<specific section URL from search result>")

### Look up error message
ENFORCED:  WebSearch("ccxt ExchangeError insufficient balance binance")

---

## 13. SESSION DISCIPLINE — /compact AND /clear

### When to /compact
ENFORCED (before any branch switch):
  /compact "focus: kelly cap fix complete"
  git checkout other-branch

ENFORCED (after major phase):
  /compact "focus: orchestrator refactor done, tests pass"

### When to /clear
ENFORCED: between completely unrelated tasks

### End of session — save state
ENFORCED:
  Edit("docs/session-notes.md",
    old_str="...",
    new_str="## Session <date>\n- Fixed: kelly fraction cap\n- Next: add drawdown guard\n- Blocker: none")

### Start of next session
ENFORCED:  Read("docs/session-notes.md")                         # <30 lines, hook allows
           # Nothing else loaded until needed

---

## HOOK DECISION TREE (what happens on each tool call)

```
Tool call fired
      │
      ▼
pre-tool.sh receives JSON on stdin
      │
      ├─ Is it accessing .claude/worktrees/?  → BLOCK (all tools)
      │
      ├─ Bash?
      │   ├─ cat file (no pipe)?              → BLOCK
      │   ├─ cat *.log?                       → BLOCK
      │   ├─ sed -i?                          → BLOCK → use Edit
      │   ├─ git reset --hard?                → BLOCK
      │   ├─ git checkout -- .?               → BLOCK
      │   ├─ git merge (bare)?                → BLOCK
      │   ├─ git log (uncapped)?              → BLOCK
      │   ├─ git diff (bare)?                 → BLOCK
      │   ├─ find . -type f (bare)?           → BLOCK
      │   ├─ pip list / pip freeze?           → BLOCK
      │   ├─ pytest (uncapped)?               → BLOCK
      │   ├─ docker build (uncapped)?         → BLOCK
      │   ├─ cat .env?                        → BLOCK
      │   └─ everything else?                 → ALLOW
      │
      ├─ Read?
      │   ├─ file <30 lines?                  → ALLOW
      │   ├─ no offset+limit, file ≥30 lines? → BLOCK
      │   ├─ limit >100?                      → BLOCK
      │   └─ offset+limit set, ≤100?          → ALLOW
      │
      ├─ Write?
      │   ├─ new file?                        → ALLOW
      │   ├─ existing <50 lines?              → ALLOW
      │   └─ existing ≥50, new >100 lines?    → BLOCK → use Edit
      │
      ├─ Glob?
      │   ├─ **/* or * bare?                  → BLOCK
      │   └─ filtered (*.py, src/**)?         → ALLOW
      │
      ├─ Task?                                → BLOCK (HARD RULES: no sub-agents)
      │
      ├─ NotebookRead?
      │   ├─ no cell range?                   → BLOCK
      │   └─ with cell_start + cell_end?      → ALLOW
      │
      ├─ WebFetch?
      │   ├─ large index URL?                 → BLOCK → WebSearch first
      │   └─ specific section URL?            → ALLOW
      │
      └─ Edit / MultiEdit / Grep / LS /
         WebSearch / TodoRead / TodoWrite?    → ALLOW (efficient by design)
```

---

## WHAT NEVER ENTERS CONTEXT

- Hook script content (.claude/hooks/pre-tool.sh)
- .claudeignore content
- settings.json content
- .claude/worktrees/ anything
- logs/ full content
- data/ anything
- .venv/ anything
- __pycache__ anything
- requirements.lock
- Deny messages (appear as tool errors, not context)
- PostToolUse annotations (none installed — removed)
