# Trade-Bot — Claude Code

## Cost discipline — non-negotiable, applies to every action

Context is re-read every turn, so a wasteful tool choice on turn 10 is
still being paid for on turn 60. Pick the cheapest tool that fully does the
job. Full reasoning: the `efficient-execution` skill.

- **Edit files with `Edit`/`Write`, never with shell heredocs, `sed -i`, or
  Python rewrite scripts.** Editing outside the Edit tool makes the harness
  re-inject the entire file into context — thousands of tokens for a
  three-line change, repeated on every such edit. `src/engine/orchestrator.py`,
  `src/engine/signal_engine.py` and `src/api/main.py` are all 900+ lines;
  treat a heredoc edit to any of them as a defect. A script is justified only
  for a genuinely mechanical change across many files.
- **`Read` with `offset`/`limit`, or `Grep -n` then read around the hit.**
  Never re-read a file to confirm an edit landed — `Edit` fails loudly.
- **Never re-run a check whose inputs have not changed.** If you just fixed
  the only violation it reported, it passes.
- **Constrain greps** with `--include`, a directory, `-l`, or `head`.
- **Say it once.** The durable "why" belongs in the code comment or the
  commit body, not in all three of comment, commit, and chat reply. Keep
  commit bodies to the reasoning that is not obvious from the diff.
- **Missing capability → build it.** If something is repetitive, fiddly, or
  error-prone and will recur, pause, create a script in `scripts/` or a skill
  via `skill-creator`, use it on the current task, then resume where you
  paused. Skip this when building it would cost more than it saves — say so.
- **Stop when marginal value drops below marginal cost**, and say that
  plainly instead of continuing to find ever-smaller items.

## SESSION START (always run first)
```bash
git log --oneline -5
gh workflow run ci.yml --ref $(git branch --show-current)
```
Stack: Python 3.11 | uv | FastAPI | XGBoost+HMM | ccxt (Binance/OKX) | pytest | ruff+mypy
Entry: `src/api/main.py` | Engine: `src/engine/orchestrator.py` | Risk: `src/risk/kelly.py` | Regime: `src/regime/detector.py`

## Decision Authority
**Decide without asking:** refactors, tests, non-breaking deps, docs, lint/type fixes, commits/pushes.

**Ask first (enforced by settings.json deny/ask lists):**
`.env*`, `TRADING_MODE=live`, `execution-mode` toggles, force-push, deleting `src/execution/*` or `src/risk/*`, editing `.claude/settings*.json` / `.claude/hooks/**` / `.github/workflows/**`.

## Architecture Governance — `crypto-architect`

The `crypto-architect` skill (`.claude/skills/crypto-architect/`) is the
authority on design in this repo: 13 architectural laws plus 13 on-demand
domain references. It outranks convenience — if a change conflicts with a law,
the change is wrong, not the law.

- **Invoke it before designing or reviewing** anything touching execution,
  risk, signals, keys, models, compliance, or exchange integration. Load only
  the `references/` files the task needs; they are large.
- **Machine gate:** `scripts/arch_gate.sh` runs the law validator over `src/`
  and is wired into CI (`architecture` job, SARIF → code scanning) and
  pre-commit. It fails on HIGH+ findings.
- **Baseline:** `config/arch_baseline.json` holds the findings that pre-date
  the gate (58 at install, 52 now), so only *new* violations fail. It is
  accepted debt, not approval — the remaining CRITICAL entries (no VaR/CVaR in
  `src/risk/cognitive_engine.py`, no wash-trade guard anywhere) are real gaps
  to close, and closing them means deleting lines from the baseline. The LAW3
  idempotency CRITICALs were closed that way: every order path now submits
  through `src/execution/idempotency.py`.
- **Never widen the baseline to make a red gate green.** Refresh it only with
  `scripts/arch_gate.sh --refresh-baseline` after a deliberate decision, and
  review the diff.
- Suppress a specific false positive with a `# noqa:arch` comment on the line,
  not by loosening the pattern.

## Domain Priors
- Execution: fees, slippage, partial fills, latency, reconnects.
- Risk: Kelly is a ceiling, not a target; enforce drawdown and position limits.
- Regime: HMM transitions are probabilistic; no hard-coded regime logic.
- Crypto: funding, liquidations, basis risk, exchange solvency, rate limits.
- Data: UTC timestamps; OHLCV gaps are real, not artifacts.
- Validate signals out-of-sample; in-sample metrics alone are not sufficient.

## Hard Rules
- **Never idle-wait for a started task.** Once a CI run, background job, or
  long command is running, move immediately to the next useful work. Leave a
  signal you can check later — `run_in_background`, a dispatched run id, a
  marker file — and come back only after its expected completion time.
  Polling in a tight loop, or sitting idle until something finishes, is a
  defect.
- **Never stop while the repo still has work.** If the active branch has
  nothing left, move to the next branch and work there; if that one is clean
  too, keep moving. Stop only when there is no outstanding work anywhere in
  the project, and say so plainly.
- **Read only the lines you need from any log — local or cloud, no
  exceptions.** `gh run view --log-failed`, `grep -m5`, `tail -30`, `Read`
  with `offset`/`limit`. Never load a whole log, job output, or file to find
  a few lines.
- `uv run` for Python execution; never bare `python3`, never `pip`.
- Do not read: `.env`, `.venv/`, `data/`, `logs/`, `models/`, `requirements.lock`, `rag.db`.
- **Never run tests, lint, type-check, or build locally.** CI only: push → `gh workflow run ci.yml --ref <branch>` → poll `gh run view`. Never `uv run pytest/ruff/mypy` locally.
- **CI runners are cloud-first with a self-hosted fallback.** `ci.yml` uses `runs-on: ${{ vars.CI_RUNNER || 'ubuntu-latest' }}`. If the cloud tier is unusable — minutes exhausted, billing block, or every job failing in ~2s with `runner=null` — switch with `gh variable set CI_RUNNER --body trade-bot-selfhosted`, and switch back with `gh variable delete CI_RUNNER` once it recovers. Never leave CI pinned to the self-hosted runner. Setup, service commands and its limits (shared workspace, one job at a time, Docker needed for the `backend` job's TimescaleDB service): `docs/CI_RUNNERS.md`.
- Coverage gate: 95% global (`--cov-fail-under=95`). Per-file floors enforced by `scripts/check_coverage_floors.py` in CI for `src/execution/`, `src/engine/`, `runtime_monitor`.
- Never use the Agent tool (sub-agents) — burns 3-5× tokens; use Bash/Read/grep directly.
- No destructive operations without explicit authorization.

# === TRADE-BOT PROJECT CONTEXT ===
# This file is the project-level layer. It does NOT repeat global rules.
# Global rules live in ~/.claude/CLAUDE.md (ADOS + HARD LIMITS).
# Both files are always active. Both apply to main agent and all sub-agents.
#
# SUB-AGENT REMINDER: if you are a spawned agent reading this file,
# you are bound by ALL of the following:
#   1. ~/.claude/CLAUDE.md → ADOS (decision engine + correctness self-audit)
#   2. ~/.claude/CLAUDE.md → HARD LIMITS & AGENT PROTOCOL (enforcement rules)
#   3. This file → project-specific patterns
# All three apply. No exceptions. No drift. No partial inheritance.

## STACK FINGERPRINT
- Language  : Python (primary)
- Domain    : Algorithmic trading / market data
- Test cmd  : pytest 2>&1 | tail -n 30
- Lint cmd  : ruff check . 2>&1 | head -n 20  (or flake8 | head -20)
- Entry pts : grep -rn "def main\|if __name__" src/ --include="*.py" -l

## HIGH-NOISE PATHS — NEVER READ UNLESS TARGETED
- logs/          : tail -n 40 or grep -m 20 only
- data/          : never explore broadly; always target specific file/date
- __pycache__/   : ignore entirely
- .venv/ / venv/ : use pip show <pkg>, never browse

## COMMON TARGETED READS — USE THESE PATTERNS
- Config value  : grep -n "KEY_NAME" config*.yaml | head -5
- Failure cause : grep -m 15 "ERROR\|Exception\|Traceback" logs/*.log
- Class/fn loc  : grep -n "class FooBar\|def bar_fn" src/**/*.py | head -10
- Import check  : grep -n "^from\|^import" src/module.py | head -20

## SESSION DISCIPLINE
- /compact after: architecture decisions, implementation complete, test suite run
- /clear between: unrelated features or context clearly stale
- session-notes : end each session → ask Claude to write docs/session-notes.md
  (max 20 lines: decisions made, next TODOs, blockers)
- Next session  : load docs/session-notes.md first, nothing else

## ACTION → EFFICIENT TOOL MAP
| Goal                        | Use                                              |
|-----------------------------|--------------------------------------------------|
| Read 1 value from config    | grep -n "key" config.yaml \| head -3             |
| Fix 1 line in source        | Edit(old_str, new_str) — no file read needed     |
| Find where fn is defined    | grep -rn "def fn_name" src/ --include="*.py"     |
| Check test failure reason   | pytest 2>&1 \| tail -n 30                        |
| Check which files changed   | git diff --stat HEAD                             |
| Read specific log error     | grep -m 15 "ERROR" logs/app.log                  |
| Find file by name           | find . -name "target.py" -maxdepth 4             |
| Understand module exports   | grep -n "^class\|^def\|^__all__" module.py       |
