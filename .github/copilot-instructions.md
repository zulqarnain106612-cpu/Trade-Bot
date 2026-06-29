# GitHub Copilot — Trade Bot Workspace Instructions
# ===================================================
# Copilot reads this file automatically for every workspace session.
# These instructions make Copilot behave identically to Claude in this project.

---

## STEP 1 — SESSION START (do this before anything else)

Read these files in order — **HANDOFF.md first**:

```
.project-intel/HANDOFF.md          ← FIRST: exact state left by previous agent
.project-intel/CONTEXT_PRIMER.md   ← full project understanding
.project-intel/SESSION_STATE.json  ← current progress
.project-intel/DECISION_LOG.md     ← decisions already made
```

If HANDOFF.md shows `INTERRUPTED` — resume from the exact point shown. Do not restart.

Then register yourself:
```bash
python3 .project-intel/scripts/handoff.py start --agent copilot --task "your task"
```

Checkpoint during work:
```bash
python3 .project-intel/scripts/handoff.py checkpoint --agent copilot \
  --completed "what you just did" --next "exact next step" --files "src/file.py"
```

Finish:
```bash
python3 .project-intel/scripts/handoff.py finish --agent copilot \
  --completed "summary" --next "TASK-XXX: next task"
```

If you need to know what a module does: read `.project-intel/MODULE_MAP.json` They replace reading source code.

```
.project-intel/CONTEXT_PRIMER.md   — full project understanding + routing protocol
.project-intel/SESSION_STATE.json  — current progress, last commit, next task
.project-intel/DECISION_LOG.md     — decisions already made (do not re-debate)
```

If you need to know what a module does: read `.project-intel/MODULE_MAP.json`
If you need relevant source chunks: run `python3 .project-intel/scripts/rag_engine.py --query "your topic" --project .`
If you need domain knowledge (Kelly, HMM, CVaR, ECDSA): run `python3 .project-intel/scripts/cognitive_layer.py --query "topic" --dir .project-intel/knowledge/`

**Never read a source file to understand the project. Only open a file when you are about to edit it.**

---

## STEP 2 — OUTPUT ROUTING PROTOCOL (every response, no exceptions)

Tag all output. The system auto-routes tagged content to the correct project file.

### Tags that go to project files (never repeat in chat):
```
<gap>architecture gap or design hole</gap>               → .project-intel/GAPS.md
<issue>bug, error, or broken behavior</issue>            → .project-intel/ISSUES.md
<broken>non-functional component</broken>                → .project-intel/BROKEN.md
<missing>feature that does not exist yet</missing>       → .project-intel/MISSING.md
<decision>architecture decision made</decision>          → .project-intel/DECISION_LOG.md
<task>implementation task identified</task>              → .project-intel/OPEN_TASKS.md
<risk>risk or threat identified</risk>                   → .project-intel/RISK_LOG.md
<diagnostic>diagnostic finding or analysis</diagnostic>  → .project-intel/DIAGNOSTICS.md
<security>security issue or vulnerability</security>     → .project-intel/SECURITY_ISSUES.md
<debt>technical debt</debt>                              → .project-intel/TECH_DEBT.md
```

### Tags that go to chat:
```
<chat>conversational reply, code, explanation</chat>
Untagged content → chat
```

### Example response structure:
```
<gap>
GAP-015: No rate limiting on WebSocket connections in api/main.py.
A flood of WS connections will exhaust memory. Severity: Medium.
File: src/api/main.py
</gap>

<task>
TASK-011: Add asyncio.Semaphore(50) WS connection limiter in main.py
Wire into: websocket_endpoint() before accept()
</task>

<chat>
Found a WebSocket rate limiting gap — logged to GAPS.md, task in OPEN_TASKS.md.
Here is the fix: [code follows]
</chat>
```

**Rules:**
- Never write gaps/issues/tasks/risks as plain chat text — always tag them
- Never duplicate tagged content in chat
- Always end with a `<chat>` summary after any project-bound blocks

---

## STEP 3 — COMMIT PROTOCOL

After every meaningful change, commit using the project commit script:

```bash
bash scripts/claude-commit.sh --msg "type(scope): description [copilot]"
```

Commit types: `feat` `fix` `refactor` `chore` `docs` `test` `security` `perf` `audit`

**Never push.** Push is manual, controlled by the human only.

---

## STEP 4 — NEVER DO THESE

- Do NOT read entire source files to understand the project
- Do NOT ask the user to explain the project — it is in CONTEXT_PRIMER.md
- Do NOT re-read files already read — use SESSION_STATE.json
- Do NOT push to git — commit only
- Do NOT write gaps/issues as plain chat — use XML tags
- Do NOT run `scripts/vulner_fix_append.py` — archived, use `<security>` tags
- Do NOT use `print()` in `src/` — use structlog
- Do NOT bypass `CognitiveEngine` in signal_engine.py — all 5 validators are mandatory
- Do NOT hardcode secrets or credentials

---

## PROJECT IDENTITY

Production algorithmic trading bot.
**Stack**: Python 3.11 + FastAPI + XGBoost + GaussianHMM + React + SQLite WAL
**Exchanges**: Binance (primary), OKX (secondary). Paper-first, live-gated.

### Signal flow (immutable)
```
Exchange → fetch → store → features → regime → models
→ CognitiveEngine (MANDATORY — 5 validators, no bypass)
→ filters → sizing → gates → executor → api → dashboard
```

### CognitiveEngine — mandatory runtime validators
Every signal passes ALL five. Single VETO kills the trade:
```
QuantValidator       Kelly math, CPCV Sharpe, sizing bounds
ProbabilityValidator Bayesian posterior, CVaR, Monte Carlo (1000 paths)
RiskValidator        STRIDE threats, drawdown, volatility explosion
BlockchainValidator  Exchange trust, funding rate, basis divergence
RegimeValidator      HMM entropy gate, Hurst persistence
```
File: `src/risk/cognitive_engine.py` — modify only with full understanding of all 5 validators.

### Risk gates (sequential, first fail short-circuits)
```
Gate-0  SlippageModel negative-EV veto
Gate-1  CognitiveEngine 5-validator pass
Gate-2  Daily drawdown > 2% halt
Gate-3  Consecutive losses >= 3 halt
Gate-4  Regime = volatile block
Gate-5  Max position > 5% capital
Gate-6  Paper < 30 days
Gate-7  Live gate: OOS Sharpe>1.5, DD<15%, 500+ trades
```

### Module map
```
src/config.py                      Settings, enums, RuntimeConfig, HMMSettings
src/data/fetcher.py                ccxt OHLCV + orderbook (1m/15m/4h)
src/data/storage.py                Async SQLite WAL
src/features/pipeline.py           7 features + triple-barrier + CPCV
src/regime/detector.py             GaussianHMM 3-state + entropy gate
src/models/trainer.py              XGBoost direction + meta-label
src/risk/cognitive_engine.py       5-validator mandatory decision engine
src/risk/kelly.py                  Half-Kelly (0.5×, 0.25 ceiling)
src/risk/gates.py                  Sequential hard risk gates
src/risk/slippage.py               Almgren-Chriss slippage model
src/execution/paper.py             Paper executor (Auto/Restricted/Manual)
src/execution/live.py              Live executor + order FSM
src/execution/order_fsm.py         Order state machine
src/strategies/filters.py          8 signal filters
src/strategies/position_sizing.py  Carver + AFML + Thorp sizing
src/engine/signal_engine.py        Per-timeframe pipeline
src/engine/orchestrator.py         Async event loop
src/api/main.py                    FastAPI REST + WebSocket
src/diagnostics/                   RuntimeMonitor, SignalDebugger, TradeAuditor, LabelShiftDetector
frontend/src/App.jsx               React dashboard
```

### Key constants (always use get_settings() — never hardcode)
```
DAILY_DD_HALT=2%  CONSECUTIVE_LOSS_HALT=3  MAX_POSITION_PCT=5%
KELLY_MULTIPLIER=0.5  KELLY_CEILING=0.25  PAPER_MIN_DAYS=30
LIVE_SHARPE_MIN=1.5  LIVE_MAX_DD=15%  LIVE_MIN_TRADES=500
PRIMARY_TIMEFRAME=15m
```

---

## CODE GENERATION RULES

### Always
- `from __future__ import annotations` at top of every module
- `async def` for all I/O-touching functions
- `structlog.get_logger(__name__)` for logging
- Parameterised SQL: `conn.execute("WHERE x=?", (val,))`
- Type annotations on all function signatures
- `dataclasses.dataclass(frozen=True)` for value objects
- `asyncio.Lock` for shared mutable state
- Cite authority in docstrings: `# Kelly (1956) Bell System Technical Journal 35(4)`
- `get_settings()` for all config — never `os.getenv()`

### Never
- `print()` in `src/`
- Bare `except:`
- `time.sleep()` in async — use `await asyncio.sleep()`
- Blocking I/O on event loop — use `asyncio.to_thread()`
- `import *`
- f-string SQL interpolation
- `pd.DataFrame.iterrows()`

### Lock-snapshot pattern
```python
async with self._lock:
    self._state = new_value
    snap = self._state  # capture inside lock
# use snap outside — never re-read self._state after unlock
```

### New API endpoint pattern
```python
@app.get("/endpoint", dependencies=[Depends(api_key_header)])
async def endpoint(request: Request) -> dict[str, Any]:
    _state.check_endpoint_rate_limit("endpoint", request.client.host or "")
    ...
```

---

## AUTHORITY REFERENCES
```
López de Prado (2018) AFML         — triple-barrier, CPCV, meta-labeling
Kelly (1956) Bell System Tech J    — position sizing
Hamilton (1989) Econometrica 57(2) — HMM regime detection
Peters (1994) Fractal Market Hyp   — Hurst exponent
Thorp (2006) Kelly Criterion       — variance-adjusted Kelly
Almgren & Chriss (2001)            — market impact / slippage
Carver (2019) Systematic Trading   — trend filters
Schwager (1984)                    — volatility explosion gate
```

---

## HEALTH CHECK

If anything seems wrong:
```bash
bash scripts/intel-health.sh
```
Self-heals: daemon, watchdog, shell hook, gitignore, all intel files.

---

## DOMAIN KNOWLEDGE

Available at `.project-intel/knowledge/` — Kelly, CPCV, Bayesian risk, VaR/CVaR,
Hurst, STRIDE, Almgren-Chriss, Crypto algorithms, DeFi risk, CAP theorem, SRE.

Query: `python3 .project-intel/scripts/cognitive_layer.py --query "topic" --dir .project-intel/knowledge/`
