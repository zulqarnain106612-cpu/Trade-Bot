# Trade-Bot — Claude Code

## SESSION START
Do nothing. No commands, no reads, no CI dispatch.
Wait for an explicit user message before any action, on every branch.

Stack: Python 3.11 | uv | FastAPI | XGBoost+HMM | ccxt (Binance/OKX) | pytest | ruff+mypy
Entry: `src/api/main.py` | Engine: `src/engine/orchestrator.py` | Risk: `src/risk/kelly.py` | Regime: `src/regime/detector.py`

## Decision Authority
**Decide without asking:** refactors, tests, non-breaking deps, docs, lint/type fixes, commits/pushes.

**Ask first (enforced by settings.json deny/ask lists):**
`.env*`, `TRADING_MODE=live`, `execution-mode` toggles, force-push, deleting `src/execution/*` or `src/risk/*`, editing `.claude/settings*.json` / `.claude/hooks/**` / `.github/workflows/**`.

## Domain Priors
- Execution: fees, slippage, partial fills, latency, reconnects.
- Risk: Kelly is a ceiling, not a target; enforce drawdown and position limits.
- Regime: HMM transitions are probabilistic; no hard-coded regime logic.
- Crypto: funding, liquidations, basis risk, exchange solvency, rate limits.
- Data: UTC timestamps; OHLCV gaps are real, not artifacts.
- Validate signals out-of-sample; in-sample metrics alone are not sufficient.

## Hard Rules
- `uv run` for Python execution; never bare `python3`, never `pip`.
- Do not read: `.env`, `.venv/`, `data/`, `logs/`, `models/`, `requirements.lock`, `rag.db`.
- **Never run tests, lint, type-check, or build locally.** CI only: push → `gh workflow run ci.yml --ref <branch>` → poll `gh run view`. Never `uv run pytest/ruff/mypy` locally.
- Coverage gate: 95% global (`--cov-fail-under=95`). Per-file floors enforced by `scripts/check_coverage_floors.py` in CI for `src/execution/`, `src/engine/`, `runtime_monitor`.
- Never use the Agent tool (sub-agents) — burns 3-5× tokens; use Bash/Read/grep directly.
- No destructive operations without explicit authorization.

## CI ECONOMY — cloud minutes are a budget, not a faucet

CI is the only validator, so it runs often; that makes it the largest
recurring cloud cost in the project. Every run must earn its minutes. These
rules bind every workflow change and every decision to dispatch a run.

- **Never verify the same thing twice.** Validation is keyed on *content*,
  not on branch name. If a tree SHA has already gone green it is verified —
  do not re-run it because it now sits on a different branch, or because a
  merge commit was created around unchanged content.
- **Shared setup runs once, not once per branch.** Dependency resolution,
  the uv/pip install, and any model or fixture download are common to every
  branch. Cache them keyed by the lockfile hash (`requirements.lock`,
  `frontend/package-lock.json`) so N branches pay that cost once, not N
  times. A cache miss on an unchanged lockfile is a defect, not bad luck.
- **Only test what changed.** Gate jobs on path filters: a docs-only or
  config-only diff must not run the full pytest matrix, and a backend diff
  must not run the frontend build. Reviewing N branches that share a base
  means reviewing the *deltas*, not re-reviewing the shared base N times.
- **Never dispatch speculatively.** No "let's see if it passes" runs, no
  re-dispatch to get fresher logs, no dispatching a branch whose code is
  identical to one already verified. Read the existing run first.
- **Re-dispatch cancels the queued run** (`concurrency: ci-${{ github.ref }}`,
  `cancel-in-progress: true`). Re-dispatching to "retry" destroys the
  in-flight run and buys nothing — wait for the result instead.
- **Batch related branches.** When several branches need the same
  verification, validate the merged result once rather than each branch
  separately.
- **A red run is information — mine it before spending another.** Fix every
  failure it reported in one pass; do not spend a run per fix.

Before adding any workflow or job, state what it costs per run and what it
catches that an existing job does not. If it cannot answer both, it does not
get added.
