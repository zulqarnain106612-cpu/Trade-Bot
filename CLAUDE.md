# Trade-Bot — Claude Code

## SESSION START
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
