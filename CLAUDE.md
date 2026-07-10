# Trade-Bot — Claude Code Session

## ⚡ SESSION START
```bash
python3 .project-intel/scripts/resume.py /home/fujitsu/Projects/Trade-Bot-main
```
This output IS your full context. Stop. Ask what to work on.

## Fallback (resume.py fails)
```bash
uv run pytest tests/ -x -q
git log --oneline -5
python3 -c "import json; s=json.load(open('.project-intel/SESSION_STATE.json')); print('NEXT:', s['next_recommended_task'][:200])"
```
Stack: Python 3.11 | uv | FastAPI | XGBoost+HMM | ccxt (Binance/OKX) | pytest | ruff + mypy
Entry: `src/api/main.py` | Engine: `src/engine/orchestrator.py` | Risk: `src/risk/kelly.py` | Regime: `src/regime/detector.py`

## Operating Mode — Conqueror
- Production-critical by default. Priorities: correctness → determinism → security → scalability → maintainability → reproducibility → stability → performance → token efficiency.
- Before acting: use local tools/connectors first, prefer low-token local execution, and validate intent, context, environment, dependencies, compatibility, feasibility, security, rollback, and edge cases. Recover automatically; ask only if blocked.
- Implement with evidence, not guesses. No hallucinations, hidden failures, skipped validation, or fragile patches. Assumptions are allowed only for risk analysis and mitigation.
- Preserve backward compatibility, rollback, modularity, observability, extensibility, fault tolerance, and low technical debt. Predict bottlenecks, race conditions, state corruption, resource exhaustion, dependency conflicts, and security issues before acting.
- Security by default: protect secrets, validate permissions, sandbox limits, input/output safety, and dependency trust; avoid destructive actions without explicit authorization.
- Optimize for token and runtime efficiency: minimize redundancy, latency, complexity, and overhead while maximizing robustness, automation, continuity, and maintainability.
- Work in small, independent steps; chunk generated files to ≤30 lines; keep chat concise, exact, and implementation-focused; use commits/history for tracking instead of verbose logs. Structure work for continuation across sessions.

## Domain Priors
- Execution: account for fees, slippage, partial fills, latency, and reconnects.
- Risk: Kelly is a ceiling, not a target; enforce drawdown and position limits.
- Regime: treat HMM transitions as probabilistic; avoid hard-coded regime logic.
- Crypto: account for funding, liquidations, basis risk, exchange solvency, and rate limits.
- Data: normalize timestamps to UTC; treat OHLCV gaps as real.

## Probabilistic Workflow
- Use probabilistic reasoning end to end: data → features → regime/model → risk → execution.
- Prefer robust methods and verified references over heuristics; verify outputs before acting.
- Validate signals with out-of-sample quality metrics; do not rely on in-sample metrics alone.

## Hard Rules
- Use resume.py and grep/sed for context; do not read files to orient.
- Do not read: .env, GAPS.md, ARCHITECTURE.md, MODULE_MAP.json, RAW_SCAN.json, SESSION_STATE.json, rag.db, requirements.lock, .venv/, data/, logs/, models/.
- Use uv for Python execution; avoid bare python3 or pip.
- Validate before claiming success: uv run ruff check --fix src/ && uv run mypy src/ && uv run pytest tests/ -x -q.
- Never perform destructive operations without explicit authorization.
- Coverage gate: global minimum is 95% (`--cov-fail-under=95` in pyproject.toml `[tool.pytest.ini_options]` and `fail_under = 95` in `[tool.coverage.report]`). Treat any session/task that drops coverage below 95% as incomplete; when adding or changing code, add tests to hold the line at 95%+.

## Output Style
- Minimal, exact, implementation-focused; no filler or progress narration.
- Prefer one complete solution over multiple weak alternatives.
- Keep code in files, not chat, unless the snippet is short.
