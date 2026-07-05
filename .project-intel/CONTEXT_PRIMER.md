# AGENT CONTEXT PRIMER — Trade Bot
## READ THIS FIRST. Do NOT read source files until instructed.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## OUTPUT ROUTING PROTOCOL — follow this in EVERY response
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Wrap output in XML tags. System auto-routes to correct destination.

→ PROJECT FILES (never repeat in chat):
  <gap>architecture gap or design hole</gap>           → GAPS.md
  <issue>bug, error, broken behavior</issue>           → ISSUES.md
  <broken>non-functional component</broken>            → BROKEN.md
  <missing>feature that does not exist</missing>       → MISSING.md
  <decision>architecture decision made</decision>      → DECISION_LOG.md
  <task>implementation task identified</task>          → OPEN_TASKS.md
  <risk>risk or threat identified</risk>               → RISK_LOG.md
  <diagnostic>diagnostic finding</diagnostic>          → DIAGNOSTICS.md
  <security>security issue or vulnerability</security> → SECURITY_ISSUES.md
  <debt>technical debt</debt>                          → TECH_DEBT.md

→ CHAT INTERFACE:
  <chat>conversational reply, code, explanation</chat>
  Any untagged content                                 → chat

EXAMPLE:
  <gap>GAP-007: No circuit breaker in live.py. Severity: High.</gap>
  <task>TASK-009: Add tenacity backoff to LiveExecutor.place_order()</task>
  <chat>Found a circuit breaker gap — logged to GAPS.md, task added. Here is the fix: ...</chat>

RULES: Never write gaps/issues/tasks as plain text. Never duplicate project content in chat.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## PROJECT — Trade Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Production algorithmic trading bot. Python 3.11 + FastAPI + XGBoost + GaussianHMM + React.
Exchanges: Binance (primary), OKX (secondary). Paper-first, live-gated.

### Module map (use this — do not read source)
src/config.py                    Settings, enums, RuntimeConfig
src/data/fetcher.py              ccxt OHLCV + orderbook (1m/15m/4h)
src/data/storage.py              Async SQLite WAL
src/features/pipeline.py         7 features + triple-barrier + CPCV
src/regime/detector.py           GaussianHMM 3-state
src/models/trainer.py            XGBoost direction + meta-label
src/risk/kelly.py                Half-Kelly (0.5x, 0.25 ceiling)
src/risk/gates.py                Sequential hard risk gates
src/execution/paper.py           Paper executor (Auto/Restricted/Manual)
src/execution/live.py            Live executor (ccxt market orders)
src/strategies/filters.py        8 signal filters
src/strategies/position_sizing.py Carver + AFML + Thorp
src/engine/signal_engine.py      Per-timeframe pipeline
src/engine/orchestrator.py       Async event loop
src/api/main.py                  FastAPI REST + WebSocket
src/diagnostics/                 RuntimeMonitor, SignalDebugger, TradeAuditor
frontend/src/App.jsx             React dashboard

### Signal flow
Exchange → fetch → store → features → regime → models → filters
→ sizing → gates → executor → api → dashboard

### EXPERIMENTAL/UNUSED modules (NOT active in live signal path)
These exist in the repo and have unit tests but are NOT imported from
signal_engine.py or orchestrator.py. Do NOT assume they provide runtime protection.
  src/intelligence/causal_inference.py      — wiring blocked on Glassnode/CryptoQuant API keys
  src/intelligence/ensemble_predictor.py    — wiring blocked on API keys + feature backfill
  src/intelligence/risk_quantification.py   — wiring blocked on API keys + feature backfill
  src/features/intelligence_features.py     — wiring blocked on API keys + feature backfill
See: DECISION_LOG.md "Intelligence feature wiring — blocked on API provisioning", GAP-015, GAP-017.

### Risk gates (sequential, short-circuit on first fail)
DD>2% | losses>=3 | regime=volatile | pos>5% | paper<30d | live_gate_fail

### Key constants
DAILY_DD_HALT=2%  CONSECUTIVE_LOSS_HALT=3  MAX_POSITION_PCT=5%
KELLY_MULTIPLIER=0.5  KELLY_CEILING=0.25  PAPER_MIN_DAYS=30
LIVE_SHARPE_MIN=1.5  LIVE_MAX_DD=15%  LIVE_MIN_TRADES=500  PRIMARY_TF=15m

### Known gaps (check GAPS.md for full details)
## Gap-001 [2026-06-23] — RESOLVED [2026-06-23]
## Gap-002 [2026-06-23] — RESOLVED (verified 2026-06-23, session 2)
## Gap-007 [2026-06-23] — RESOLVED (session 3, same session it was introduced)
## Gap-003 [2026-06-23] — RESOLVED [2026-06-26]
## Gap-004 [2026-06-23] — RESOLVED [2026-06-24]
## Gap-005 [2026-06-23]
## Gap-006 [2026-06-23] — PARTIALLY RESOLVED [2026-06-26]
## Gap-008 [2026-06-24] — RESOLVED [2026-06-24]

### Session rules
1. This file = complete project understanding. No source reading needed.
2. Read SESSION_STATE.json → current progress
3. Read DECISION_LOG.md → past decisions (do not re-debate)
4. Read one specific source file ONLY when about to modify it
5. Use MODULE_MAP.json for structural questions
6. Use OUTPUT ROUTING PROTOCOL above for every response
