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
- `uv run` for Python execution; never bare `python3`, never `pip`.
- Do not read: `.env`, `.venv/`, `data/`, `logs/`, `models/`, `requirements.lock`, `rag.db`.
- **Never run tests, lint, type-check, or build locally.** CI only: push → `gh workflow run ci.yml --ref <branch>` → poll `gh run view`. Never `uv run pytest/ruff/mypy` locally.
- **CI runners are cloud-first with a self-hosted fallback.** `ci.yml` uses `runs-on: ${{ vars.CI_RUNNER || 'ubuntu-latest' }}`. If the cloud tier is unusable — minutes exhausted, billing block, or every job failing in ~2s with `runner=null` — switch with `gh variable set CI_RUNNER --body trade-bot-selfhosted`, and switch back with `gh variable delete CI_RUNNER` once it recovers. Never leave CI pinned to the self-hosted runner. Setup, service commands and its limits (shared workspace, one job at a time, Docker needed for the `backend` job's TimescaleDB service): `docs/CI_RUNNERS.md`.
- Coverage gate: 95% global (`--cov-fail-under=95`). Per-file floors enforced by `scripts/check_coverage_floors.py` in CI for `src/execution/`, `src/engine/`, `runtime_monitor`.
- Never use the Agent tool (sub-agents) — burns 3-5× tokens; use Bash/Read/grep directly.
- No destructive operations without explicit authorization.
