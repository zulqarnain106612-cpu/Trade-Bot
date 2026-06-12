# Trade-Bot — Claude Project Configuration

## Project Identity
Async Python algorithmic trading bot. FastAPI backend, XGBoost direction model,
HMM regime detector, paper + live (ccxt) execution, SQLite/aiosqlite storage.

## Stack
- Python 3.11, FastAPI, uvicorn, aiosqlite, ccxt, xgboost, hmmlearn
- structlog for all logging — NEVER use print() or logging.getLogger()
- pydantic-settings for config — NEVER os.getenv() directly in business logic
- asyncio throughout — NEVER blocking I/O on the event loop
- pandas/numpy for data — NEVER raw loops over DataFrames

## Vulner-Fix.md protocol (MANDATORY)
When you find ANY issue, vulnerability, warning, or error during any task:

1. **Do NOT list findings in chat** — write to `Vulner-Fix.md` instead
2. Run: `python scripts/vulner_fix_append.py --severity X --tool claude --file "path:line" --summary "..." --fix "..." --status Open`
3. After successfully applying a fix: `python scripts/vulner_fix_append.py --mark-applied VF-NNN`
4. NEVER overwrite or reformat existing `Vulner-Fix.md` content — append only
5. Each new finding goes to the last line of the file
6. Status flow: `Open` → `In Progress` → `Applied`

## Architecture
```
src/
  api/          main.py — FastAPI app, WebSocket, debug endpoints
  config.py     — Settings (pydantic), runtime_config (async mutable)
  data/         fetcher.py, storage.py (aiosqlite)
  diagnostics/  runtime_monitor.py, trade_auditor.py, signal_debugger.py
  engine/       orchestrator.py (event loop), signal_engine.py (per-tick)
  execution/    base.py, paper.py, live.py (ccxt)
  features/     pipeline.py (build_feature_matrix, build_inference_features)
  models/       trainer.py (XGBoost CPCV)
  regime/       detector.py (HMM)
  risk/         gates.py, kelly.py
  strategies/   filters.py (8 professional filters), position_sizing.py
```

## Non-negotiable coding rules

### Security
- NEVER f-string SQL. Always parameterised queries with tuple params.
- NEVER log secrets, API keys, credentials, or raw exception tracebacks externally.
- NEVER trust user input without validation — use pydantic models or explicit guards.
- ALL financial write paths (insert_trade, update_trade_exit) use PRAGMA synchronous=FULL.
- WebSocket: authenticate BEFORE mutating any server state.

### Concurrency
- ALL shared state mutations under asyncio.Lock.
- Snapshot values INSIDE the lock before releasing — never read state after unlock.
- Use _trade_semaphore in live/paper executors to serialise open+close.
- NEVER asyncio.run() inside an async context.

### Error handling
- NEVER bare except: — always catch specific exceptions.
- NEVER swallow exceptions silently — log at appropriate level first.
- Financial failures (insert_trade, close_position) → log.critical with full context.
- Use structlog bound loggers: self._log = log.bind(component="...", symbol="...")

### Testing
- pytest-asyncio for all async tests. asyncio_mode = "auto".
- No mocks for pure functions — test with real data fixtures.
- Coverage floor: 60% (pytest --cov-fail-under=60).

### Style
- Line length 100 (ruff enforced).
- All new public functions need docstrings with Authority citation if from literature.
- Type annotations required on all public function signatures.
- dataclasses for value objects, not dicts.

## Key patterns

### Adding a new feature to the pipeline
1. Add column constant to `FEATURE_COLUMNS` in `pipeline.py`
2. Compute in `build_feature_matrix()` — vectorised pandas, no loops
3. Add to `build_inference_features()` extraction
4. Update `FeatureDriftMonitor.set_baseline()` call in `trainer.py`

### Adding a new risk gate
1. Add `GateStatus` enum value in `gates.py`
2. Add evaluation function `_check_*()` returning bool
3. Wire into `evaluate_all_gates()` chain — fail-fast ordered cheapest-first
4. Add test in `tests/test_risk_gates.py`

### Adding a new strategy filter
1. Pure function in `strategies/filters.py` — no I/O, pd.Series inputs
2. Add to `apply_all_strategy_filters()` stack
3. Add authority docstring citation
4. Add test

### Adding a new API endpoint
1. Add to `src/api/main.py` with `@app.get/post`
2. Require `api_key_header` dependency
3. Rate-limit via `_state.check_endpoint_rate_limit(endpoint, request.client.host)`
4. Input validation via pydantic model or Query with constraints

## Diagnostics
- `GET /debug/health`    — RuntimeMonitor: probes, tick-stall, memory
- `GET /debug/audit`     — TradeAuditor: last N decisions, anomaly_scan()
- `GET /debug/drift`     — FeatureDriftMonitor + ModelDegradationTracker
- `POST /debug/selftest` — pipeline synthetic round-trip

## Authority references (cite in docstrings)
- López de Prado (2018) Advances in Financial Machine Learning (AFML)
- Carver (2019) Systematic Trading
- Chan (2013) Algorithmic Trading
- Aronson (2006) Evidence-Based Technical Analysis
- Peters (1994) Fractal Market Hypothesis
- Elder (1993) Trading for a Living
- Schwager (1984/1993) Market Wizards
- Kelly (1956) Bell System Technical Journal
- Hamilton (1989) Journal of Political Economy — regime-switching

## Common mistakes to avoid
- Do NOT use `self._cfg.execution_mode` in executors — use `await runtime_config.get_execution_mode()`
- Do NOT call `_bulk_write_ctx` with a nested `async with self._lock` — lock is held internally
- Do NOT fit RegimeDetector twice — create a new instance for retraining
- Do NOT read equity/position state outside a lock in mark_to_market
- Do NOT add new bare `p_bet` locals before `_p_bet_ref[0]` is set in signal_engine.tick()
