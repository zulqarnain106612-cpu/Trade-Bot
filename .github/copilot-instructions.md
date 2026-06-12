# GitHub Copilot — Trade-Bot Workspace Instructions

## Output style (matches Claude's response format)
- Use structured prose, not bullet walls
- Lead with what you're doing, then show code
- Use tables for comparisons, not nested bullets
- Always cite authority in docstrings for algorithmic decisions
- Be concise and exact — zero filler words
- When fixing issues: show the broken snippet, then the fixed snippet
- End complex answers with a one-line summary of what changed and why

## Vulner-Fix.md protocol (MANDATORY for all agents)
When you find ANY issue, vulnerability, warning, or error:

1. **Do NOT print findings in chat** — write them to `Vulner-Fix.md` instead
2. Call `python scripts/vulner_fix_append.py` with the finding details
3. After applying a fix successfully, call `python scripts/vulner_fix_append.py --mark-applied VF-NNN`
4. Never overwrite existing content in `Vulner-Fix.md` — append only
5. New findings go to the LAST LINE of the file

Example agent workflow:
```bash
# 1. Found an issue → append it
python scripts/vulner_fix_append.py \
  --severity HIGH \
  --tool copilot \
  --file "src/api/main.py:42" \
  --summary "f-string in SQL query — injection risk" \
  --fix "Replace with parameterised query: conn.execute('... WHERE x=?', (val,))" \
  --status Open

# 2. Applied the fix → mark it
python scripts/vulner_fix_append.py --mark-applied VF-007
```

## Project context
Async Python algorithmic trading bot. Security and correctness are paramount —
this system manages real money. Every suggestion must be production-safe.

## Stack
Python 3.11, asyncio, FastAPI, aiosqlite, ccxt, xgboost, hmmlearn, pandas, numpy,
structlog (logging), pydantic-settings (config), pytest + pytest-asyncio (tests)

## Code generation rules

### Always
- `async def` for all I/O-touching functions
- `structlog.get_logger(__name__)` — never `logging.getLogger` or `print()`
- Parameterised SQL tuples — never f-string or %-format SQL
- Type annotations on all function signatures
- `dataclasses.dataclass` for value objects
- Cite authority in docstrings: `# Kelly (1956) Bell System Technical Journal`
- `from __future__ import annotations` at top of every module
- `asyncio.Lock` for all shared mutable state

### Never
- `print()` anywhere in `src/`
- `os.getenv()` in business logic — use `get_settings()`
- Bare `except:` — always catch specific exceptions
- `time.sleep()` in async — use `await asyncio.sleep()`
- Blocking file I/O on event loop — use `asyncio.to_thread()`
- `import *`
- Hard-coded secrets, API keys, or credentials
- `pd.DataFrame.iterrows()` — use vectorised pandas
- f-string SQL interpolation

## Naming conventions
- Async functions: `async def verb_noun()` — `fetch_bars`, `insert_trade`
- Private helpers: `_snake_case`
- Constants: `ALL_CAPS: Final[type] = value`
- Dataclasses: `PascalCase`, `frozen=True` when possible

## Common patterns

### New API endpoint
```python
@app.get("/my-endpoint", dependencies=[Depends(api_key_header)])
async def my_endpoint(request: Request) -> dict[str, Any]:
    _state.check_endpoint_rate_limit("my_endpoint", request.client.host or "")
    ...
```

### Executor state mutation (lock-snapshot pattern)
```python
async with self._lock:
    self._state = new_value
    snap = self._state   # capture inside lock
# use snap outside — never re-read self._state after unlock
```

### SQL (parameterised only)
```python
# CORRECT
await conn.execute("SELECT * FROM trades WHERE symbol=?", (symbol,))

# WRONG — never
await conn.execute(f"SELECT * FROM trades WHERE symbol='{symbol}'")
```

## Architecture
```
src/
  api/          main.py — FastAPI, WebSocket, /debug/* endpoints
  config.py     — Settings (pydantic), runtime_config (async mutable)
  data/         fetcher.py, storage.py (aiosqlite)
  diagnostics/  runtime_monitor.py, trade_auditor.py, signal_debugger.py
  engine/       orchestrator.py, signal_engine.py
  execution/    base.py, paper.py, live.py (ccxt)
  features/     pipeline.py (build_feature_matrix)
  models/       trainer.py (XGBoost CPCV)
  regime/       detector.py (HMM)
  risk/         gates.py, kelly.py
  strategies/   filters.py, position_sizing.py
```

## Diagnostics endpoints
- `GET  /debug/health`    — RuntimeMonitor snapshot
- `GET  /debug/audit`     — TradeAuditor decisions + anomaly_scan()
- `GET  /debug/drift`     — FeatureDriftMonitor KS report
- `POST /debug/selftest`  — pipeline round-trip self-test

## Authority references
- López de Prado (2018) AFML — model diagnostics, position sizing
- Carver (2019) Systematic Trading — trend filters, vol targeting
- Chan (2013) Algorithmic Trading — momentum, data quality
- Aronson (2006) Evidence-Based Technical Analysis — signal stationarity
- Peters (1994) Fractal Market Hypothesis — Hurst exponent
- Kelly (1956) Bell System Technical Journal — position sizing
- Hamilton (1989) — regime-switching models
