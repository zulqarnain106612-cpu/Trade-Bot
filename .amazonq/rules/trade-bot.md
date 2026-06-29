# Amazon Q — Trade Bot Workspace Rules
# =====================================
# Amazon Q reads .amazonq/rules/*.md automatically for every session.
# These rules make Amazon Q behave identically to Claude and Copilot.

---

## STEP 1 — SESSION START (mandatory before anything else)

Read these files in order — they replace reading source code:

```
.project-intel/HANDOFF.md          ← READ FIRST — shows exact state left by previous agent
.project-intel/CONTEXT_PRIMER.md   ← full project understanding + routing protocol
.project-intel/SESSION_STATE.json  ← current progress and implementation status
.project-intel/DECISION_LOG.md     ← decisions already made (do not re-debate)
```

Then register yourself:
```bash
python3 .project-intel/scripts/handoff.py start --agent amazonq --task "describe your task"
```

If HANDOFF.md shows status `INTERRUPTED` — resume from the exact point shown. Do not restart.
If HANDOFF.md shows status `ACTIVE` by another agent — check `git log --oneline -3`. If stale, take over.

**Never read a source file to understand the project. Use MODULE_MAP.json and RAG.**

---

## STEP 2 — OUTPUT ROUTING PROTOCOL (every response, no exceptions)

Tag all output. System auto-routes to correct file.

```
<gap>architecture gap or design hole</gap>               → .project-intel/GAPS.md
<issue>bug, error, or broken behavior</issue>            → .project-intel/ISSUES.md
<broken>non-functional component</broken>                → .project-intel/BROKEN.md
<missing>feature that does not exist yet</missing>       → .project-intel/MISSING.md
<decision>architecture decision made</decision>          → .project-intel/DECISION_LOG.md
<task>implementation task identified</task>              → .project-intel/OPEN_TASKS.md
<risk>risk or threat identified</risk>                   → .project-intel/RISK_LOG.md
<diagnostic>diagnostic finding</diagnostic>              → .project-intel/DIAGNOSTICS.md
<security>security issue or vulnerability</security>     → .project-intel/SECURITY_ISSUES.md
<debt>technical debt</debt>                              → .project-intel/TECH_DEBT.md
<chat>conversational reply, code, explanation</chat>     → chat only
```

Never write gaps/issues/tasks as plain chat text. Never duplicate tagged content in chat.
Always end with a `<chat>` summary after project-bound blocks.

---

## STEP 3 — CHECKPOINT AS YOU WORK

After every meaningful step (not just at end):
```bash
python3 .project-intel/scripts/handoff.py checkpoint --agent amazonq \
  --completed "what you just did" \
  --next "exact next action — file + line if possible" \
  --files "src/file1.py,src/file2.py"
```

This ensures if your session is interrupted, the next agent resumes from the right place.

---

## STEP 4 — COMMIT PROTOCOL

After each meaningful change:
```bash
bash scripts/claude-commit.sh --msg "type(scope): description [amazonq]"
```

Types: `feat` `fix` `refactor` `chore` `docs` `test` `security` `perf` `audit`
Always append `[amazonq]` to distinguish your commits from `[claude]` and `[copilot]`.
**Never push.** Push is manual, human-only.

---

## STEP 5 — SESSION END (clean finish or interrupted)

Clean finish:
```bash
python3 .project-intel/scripts/handoff.py finish --agent amazonq \
  --completed "what you fully completed" \
  --next "TASK-XXX: exact next task for the next agent"
```

If interrupted (copy this to run before closing):
```bash
python3 .project-intel/scripts/handoff.py interrupt --agent amazonq \
  --reason "describe why interrupted" \
  --next "exact step to resume from" \
  --files "files with uncommitted changes"
```

---

## NEVER DO THESE

- Do NOT read entire source files to understand the project
- Do NOT ask the user to explain what the project does
- Do NOT restart work that HANDOFF.md shows as partially done
- Do NOT push to git — commit only
- Do NOT write gaps/issues as plain chat — use XML tags
- Do NOT bypass `CognitiveEngine` in signal_engine.py
- Do NOT use `print()` in `src/` — use `structlog.get_logger(__name__)`
- Do NOT use bare `except:` — catch specific exceptions
- Do NOT use f-string SQL — parameterised queries only
- Do NOT use `os.getenv()` in business logic — use `get_settings()`
- Do NOT use `time.sleep()` in async — use `await asyncio.sleep()`

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

### CognitiveEngine — no bypass at runtime
All 5 validators must PASS. Single VETO kills the trade:
```
QuantValidator       Kelly math, sizing bounds (Kelly 1956, Thorp 2006)
ProbabilityValidator Bayesian posterior, CVaR, Monte Carlo 1000 paths
RiskValidator        STRIDE threats, drawdown, vol explosion gate
BlockchainValidator  Exchange trust, funding rate, basis divergence
RegimeValidator      HMM entropy gate, Hurst exponent (Peters 1994)
```
File: `src/risk/cognitive_engine.py`

### Risk gates (sequential, first fail short-circuits)
```
Gate-0  SlippageModel negative-EV veto       src/risk/slippage.py
Gate-1  CognitiveEngine 5-validator pass     src/risk/cognitive_engine.py
Gate-2  Daily drawdown > 2% halt
Gate-3  Consecutive losses >= 3
Gate-4  Regime = volatile block
Gate-5  Max position > 5% capital
Gate-6  Paper < 30 days minimum
Gate-7  Live gate: Sharpe>1.5, DD<15%, 500+ trades
```

### Module map
```
src/config.py                      Settings, enums, RuntimeConfig
src/data/fetcher.py                ccxt OHLCV + orderbook (1m/15m/4h)
src/data/storage.py                Async SQLite WAL
src/features/pipeline.py           7 features + triple-barrier + CPCV
src/regime/detector.py             GaussianHMM 3-state + entropy gate
src/models/trainer.py              XGBoost direction + meta-label
src/risk/cognitive_engine.py       5-validator mandatory decision engine
src/risk/kelly.py                  Half-Kelly (0.5×, 0.25 ceiling)
src/risk/gates.py                  Sequential hard risk gates
src/risk/slippage.py               Almgren-Chriss slippage model
src/execution/paper.py             Paper executor
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

### Key constants (always use get_settings())
```
DAILY_DD_HALT=2%   CONSECUTIVE_LOSS_HALT=3   MAX_POSITION_PCT=5%
KELLY_MULTIPLIER=0.5   KELLY_CEILING=0.25   PAPER_MIN_DAYS=30
LIVE_SHARPE_MIN=1.5   LIVE_MAX_DD=15%   LIVE_MIN_TRADES=500
PRIMARY_TIMEFRAME=15m
```

---

## CODE RULES

### Always
- `from __future__ import annotations` at top of every module
- `async def` for all I/O functions
- `structlog.get_logger(__name__)` for logging
- Parameterised SQL: `conn.execute("WHERE x=?", (val,))`
- Type annotations on all signatures
- `dataclasses.dataclass(frozen=True)` for value objects
- `asyncio.Lock` for shared mutable state
- Cite authority in docstrings: `# Kelly (1956) Bell System Technical Journal 35(4)`

### Lock-snapshot pattern
```python
async with self._lock:
    self._state = new_value
    snap = self._state   # capture inside lock
# use snap outside lock — never re-read self._state after unlock
```

---

## HEALTH CHECK

If anything seems wrong:
```bash
bash scripts/intel-health.sh
```

---

## AUTHORITY REFERENCES
```
López de Prado (2018) AFML         — triple-barrier, CPCV, meta-labeling
Kelly (1956) Bell System Tech J    — position sizing
Hamilton (1989) Econometrica       — HMM regime detection
Peters (1994) Fractal Market Hyp   — Hurst exponent
Thorp (2006)                       — variance-adjusted Kelly
Almgren & Chriss (2001)            — market impact / slippage
Carver (2019) Systematic Trading   — trend filters
Schwager (1984)                    — volatility explosion gate
```
