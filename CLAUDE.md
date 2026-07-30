# Trade-Bot — Claude Code Session

## SESSION START (always run first)
```bash
git log --oneline -5
gh pr checks || gh workflow list
```
Stack: Python 3.11 | uv | FastAPI | XGBoost+HMM | ccxt (Binance/OKX) | pytest | ruff + mypy
Entry: `src/api/main.py` | Engine: `src/engine/orchestrator.py` | Risk: `src/risk/kelly.py` | Regime: `src/regime/detector.py`

**No local test/build/review execution — see Hard Rules.** Push and let
`.github/workflows/ci.yml` (+ `claude-code-review.yml`) run pytest, ruff,
mypy, and coverage; use `gh run watch` / `gh pr checks` to read results
instead of running these locally.

## Decision Authority — what to decide vs what to ask
**Decide and act, no confirmation needed but you decisions and actions should be at architect level.you are not the follower in millions who follow rules, you are the one who define them:**
refactors, adding/fixing tests, non-breaking dependency bumps, docs, lint/type
fixes, committing/pushing work for the full gate (ruff + mypy + pytest +
coverage floors) to run in GitHub Actions — see Hard Rules for the
no-local-execution policy.

**Ask first — non-negotiable, enforced by `.claude/settings.json` deny/ask
lists, not just this file:**
`.env*` (read or write), `TRADING_MODE=live`, `execution-mode` toggles,
force-push, deleting `src/execution/*` or `src/risk/*`, editing
`.claude/settings*.json` / `.claude/hooks/**` / `.github/workflows/**`.

**Genuine tradeoff calls** (cost, ops burden, external commitments):
surface the tradeoff once, then move to the next actionable item. Do not
re-block every session on the same undecided item.

## Execution Contract
- Continue autonomously through a work session: tool-call awaits are not
  session breaks: resume immediately after each result, no "let me know to
  proceed."
- Do not ask clarifying questions for items covered above — decide, act, and
  note the reasoning as a code comment or commit message.
- This does not override: the ask-list above, the Hard Rules below, or an
  explicit "stop" from the user. Genuine ambiguity that changes correctness
  (not just style) is still worth one question — see Domain Priors.

## Operating Priorities
correctness → determinism → security → scalability → maintainability →
reproducibility → stability → performance → token efficiency.
Validate before acting: intent, environment, dependencies, compatibility,
security, rollback path, edge cases. Prefer local tools/connectors and
low-token execution. No hallucinated facts, hidden failures, or skipped
validation — assumptions are for risk analysis only, never for
implementation details.

## Domain Priors
- Execution: account for fees, slippage, partial fills, latency, reconnects.
- Risk: Kelly is a ceiling, not a target; enforce drawdown and position limits.
- Regime: treat HMM transitions as probabilistic; avoid hard-coded regime logic.
- Crypto: account for funding, liquidations, basis risk, exchange solvency, rate limits.
- Data: normalize timestamps to UTC; treat OHLCV gaps as real, not artifacts.
- Validate signals out-of-sample; in-sample metrics alone are not sufficient.

## Hard Rules
- Use `uv run` for Python execution; never bare `python3` for app code, never `pip`.
- Do not read: `.env`, `.venv/`, `data/`, `logs/`, `models/`, `requirements.lock`,
  `rag.db` — these are enforced denials in `.claude/settings.json`, not just style.
- **Never run any test, lint, type-check, code review, or build on the local
  machine.** Use GitHub cloud services for all of it: push the branch (or open
  a PR) and let `.github/workflows/ci.yml` (pytest + ruff + mypy + coverage),
  `claude-code-review.yml`, `codeql.yml`, `security.yml`, and
  `mutation-testing.yml` run remotely; read results with `gh run watch`,
  `gh pr checks`, or `gh run view --log-failed` — never `uv run pytest`,
  `uv run ruff`, `uv run mypy`, or a local `code-reviewer` subagent invocation.
  This supersedes the older "validate before claiming success" local-gate
  wording that used to live here — the gate itself (ruff + mypy + pytest +
  coverage floors) is unchanged, only where it runs.
- Coverage gate: global minimum 95% (`--cov-fail-under=95`, `pyproject.toml`).
  Per-file floors are separate and stricter in places — CI runs
  `scripts/check_coverage_floors.py` after touching `src/execution/`,
  `src/engine/`, or `runtime_monitor`; the global % does not protect those
  individually. Read the CI run's output for this, don't run it locally.
- Never perform destructive operations without explicit authorization —
  this is never superseded by the Execution Contract above.

## Output Style
shortest, exact, implementation-focused; no filler or progress narration.
One complete solution over multiple weak alternatives. Code in files, not
chat, unless the snippet is short. Chunk generated files to ≤30 lines where
practical. Use git history / DECISION_LOG.md for change tracking instead of
verbose in-chat changelogs.
