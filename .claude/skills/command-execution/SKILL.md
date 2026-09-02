---
name: command-execution
description: >
  Use for every terminal/shell command Claude runs, without exception.
  Never run bare bash and load raw output into context. Always declare the
  command through COMMAND_EXEC_SCHEMA and execute via common/shell_exec.run().
  Trigger on any task that involves running a shell command, script, test
  suite, linter, build tool, CLI, or subprocess of any kind.
---

# Command Execution — output-capped, schema-enforced

## Why this exists

Raw command output is unbounded. A single `pytest -v`, `pip list`, `git log`,
or `cat` on a large file burns thousands of context tokens on noise.
This skill enforces a declaration-first workflow: Claude declares what it will
run and how to filter the result **before** executing, so only the needed
lines ever enter context.

Works identically in local terminal sessions and cloud containers
(`CLAUDE_CODE_REMOTE=true`).

---

## Mandatory workflow — every shell command, no exceptions

### 1. Declare the command

```python
from common.shell_exec import run

result = run(
    {
        "command": "<exact shell string>",
        "output_policy": {
            "max_lines": 40,  # REQUIRED. Hard cap. Never > 100.
            "stream": "stdout",  # stdout | stderr | both
            "filter_mode": "tail",  # see filter_mode guide below
            "filter_expr": "",  # required for grep/regex/jq/fields/head(N)/tail(N)
            "on_empty": "ok",  # ok | error
        },
        # omit retry_policy entirely if no retry needed
        "retry_policy": {
            "max_attempts": 2,
            "retry_on": ["nonzero_exit"],
            "delay_s": 1,
        },
    }
)
```

### 2. Use only these four result fields

```python
result["filtered_output"]  # only this enters context — capped, filtered text
result["exit_code"]  # 0 = success
result["truncated"]  # True → tighten filter_expr, do NOT raise max_lines
result["attempt_count"]  # how many runs it took
```

Never paste raw subprocess output into conversation.

---

## filter_mode selection guide

| Command type                        | filter_mode | filter_expr example          |
|-------------------------------------|-------------|------------------------------|
| pytest / test runner                | `tail`      | `"30"` (last 30 lines)       |
| linter (ruff, mypy, flake8)         | `grep`      | `"error"` or `"warning"`     |
| Structured JSON output              | `fields`    | `"status,message,id"`        |
| JSON-per-line logs                  | `fields`    | `"level,msg,timestamp"`      |
| Log file / large text               | `regex`     | `"ERROR\|WARN\|CRITICAL"`    |
| Build output (make, npm run)        | `grep`      | `"error\|failed\|warn"`      |
| `pip list` / package listing        | `grep`      | `"<package_name>"`           |
| `git log` / history                 | `head`      | `"20"`                       |
| Exploratory / unknown output        | `head`      | `"30"` — then refine         |
| jq (only if jq confirmed on PATH)   | `jq`        | `".results[].status"`        |

**On cloud containers**: prefer `fields` or `regex` over `jq`. The `jq`
binary is not provisioned by `session-start.sh` and will raise a clear
RuntimeError if absent.

---

## max_lines budgets by command class

| Command class               | max_lines |
|-----------------------------|-----------|
| Test summary (pytest -q)    | 30–50     |
| Lint / type-check           | 20–40     |
| Build output                | 30–60     |
| Log tailing                 | 20–30     |
| API / CLI JSON response     | 20–40     |
| `pip install` / `npm i`     | 15–25     |
| Exploratory / unknown       | 30 max    |

Hard ceiling: **100 lines**. If 100 lines are not enough, the `filter_mode`
or `filter_expr` is wrong — fix those first.

---

## retry_on conditions

| Condition          | When to use                                              |
|--------------------|----------------------------------------------------------|
| `nonzero_exit`     | Transient failures: network, lock, race condition        |
| `empty_output`     | Command sometimes returns nothing on first run           |
| `pattern_absent`   | Output must contain a specific string to be valid        |

Max 5 attempts. **Never retry destructive commands** (rm, DROP, DELETE, truncate).

---

## What NOT to do

- Never set `max_lines` > 60 without explicit written justification.
- Never use `filter_mode=none` for commands with unbounded output (logs,
  verbose test output, package lists, git history). Use `none` only when
  output is guaranteed < 10 lines.
- If `result["truncated"]` is True → tighten `filter_expr`. Do not just
  raise `max_lines`.
- Never use `filter_mode=jq` without first confirming `jq` is on PATH
  (`which jq`). On cloud containers, use `fields` or `regex` instead.
- Never log or print `result["filtered_output"]` anywhere that re-enters
  model context as raw text.
