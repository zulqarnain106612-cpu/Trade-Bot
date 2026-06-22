# Trade Bot — Architecture Intelligence
> Auto-generated 2026-06-23 02:52 | 53 Python modules | 14,946 total lines

## System Purpose
Production algorithmic trading bot: Binance (primary) + OKX (secondary).
ML signal stack → risk gates → execution. Paper-first, live-gated.

## Signal Pipeline (data flow order)
```
Exchange OHLCV/OrderBook
  → fetcher.py          [ccxt, 1m/15m/4h]
  → storage.py          [SQLite WAL, async]
  → pipeline.py         [7 features, triple-barrier labels, CPCV]
  → detector.py         [GaussianHMM 3-state regime]
  → trainer.py          [XGBoost direction P(long) + meta-label P(bet)]
  → filters.py          [8 signal filters: EWM/Hurst/OBV/ATR/MTF]
  → signal_engine.py    [per-timeframe signal score]
  → position_sizing.py  [Half-Kelly + Carver + AFML + Thorp]
  → gates.py            [sequential hard risk gates, short-circuit]
  → paper.py / live.py  [execution: Auto/Restricted/Manual]
  → orchestrator.py     [async event loop]
  → main.py             [FastAPI + WebSocket]
  → React dashboard     [equity, positions, approvals, regime]
```

## Module Inventory

### `.project-intel/scripts/extract_intelligence.py` (643 lines)
**Purpose**: Project Intelligence Extractor
================================
Transforms a cod
**Key functions**: extra

### `.project-intel/scripts/update_session.py` (106 lines)
**Purpose**: Session State Updater
======================
Agents run this at the END of every
**Key functions**: main

### `check_imports.py` (4 lines)
**Purpose**: check_imports module

### `check_wf.py` (8 lines)
**Purpose**: check_wf module

### `ci_jobs_show.py` (10 lines)
**Purpose**: ci_jobs_show module

### `extract_resolved.py` (19 lines)
**Purpose**: extract_resolved module

### `scripts/claude_debug_analysis.py` (117 lines)
**Purpose**: scripts/claude_debug_analysis.py — Called by auto-debug.yml GitHub Action.

Read
**Key functions**: main

### `scripts/export_diagnostics.py` (154 lines)
**Purpose**: Run ruff + pyright (+ eslint if frontend/ exists) and write DIAGNOSTICS.md
at pr
**Key functions**: main

### `scripts/vulner_fix_append.py` (120 lines)
**Purpose**: scripts/vulner_fix_append.py — Append a new finding to Vulner-Fix.md.

Used by C
**Key functions**: next_

### `show_jobs.py` (9 lines)
**Purpose**: show_jobs module

### `show_lines.py` (29 lines)
**Purpose**: show_lines module

### `show_pattern.py` (41 lines)
**Purpose**: show_pattern module

### `src/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/api/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/api/auth.py` (97 lines)
**Purpose**: API authentication — API key validation for REST and WebSocket.

All routes and 
**Key functions**: verif

### `src/api/main.py` (763 lines)
**Purpose**: FastAPI dashboard API.

Security: ALL endpoints require X-API-Key header matchin
**Classes**: AppState, ResolveApprovalRequest, SetExecutionModeRequest
**Key functions**: api_k

### `src/api/middleware.py` (51 lines)
**Purpose**: CORS validation middleware — prevents wildcard + credentials misconfiguration
an
**Key functions**: valid

### `src/config.py` (535 lines)
**Purpose**: Production configuration for the algorithmic trading bot.

Authority sources:
  
**Classes**: TradingMode, ExecutionMode, Timeframe, BinanceSettings, OKXSettings, RiskSettings, HMMSettings, XGBoostSettings, FeatureSettings, StorageSettings, APISettings, Settings, RuntimeConfig
**Key functions**: get_s

### `src/data/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/data/fetcher.py` (712 lines)
**Purpose**: Async market data fetcher — Binance (primary) + OKX (secondary).

Responsibiliti
**Classes**: OrderBookSnapshot, MarketDataFetcher, _FetcherContextManager
**Key functions**: open_

### `src/data/storage.py` (1193 lines)
**Purpose**: Async SQLite storage layer — aiosqlite, WAL mode, typed queries.

Schema owns fi
**Classes**: BarRecord, TradeRecord, RegimeSnapshotRecord, ModelMetricsRecord, EquityRecord, StorageBackend

### `src/diagnostics/__init__.py` (0 lines)
**Purpose**: __init__ module

### `src/diagnostics/runtime_monitor.py` (307 lines)
**Purpose**: Runtime Monitor — continuous async health diagnostics with auto-healing.

Respon
**Classes**: ProbeResult, HealthSnapshot, RuntimeMonitor
**Key functions**: get_m

### `src/diagnostics/signal_debugger.py` (300 lines)
**Purpose**: Signal Debugger — feature drift detection, model degradation scanner,
          
**Classes**: FeatureDriftRecord, FeatureDriftMonitor, PredictionRecord, ModelDegradationTracker
**Key functions**: run_p

### `src/diagnostics/trade_auditor.py` (270 lines)
**Purpose**: Trade Auditor — captures every signal decision with full diagnostic context.

Ev
**Classes**: AuditRecord, TradeAuditor
**Key functions**: get_a

### `src/engine/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/engine/orchestrator.py` (579 lines)
**Purpose**: Orchestrator — top-level async event loop coordinating all subsystems.

Responsi
**Classes**: Orchestrator

### `src/engine/signal_engine.py` (507 lines)
**Purpose**: Signal engine — per-timeframe signal computation pipeline.

On every tick for a 
**Classes**: SignalResult, SignalEngine

### `src/execution/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/execution/base.py` (91 lines)
**Purpose**: Abstract base class for trade executors (VUL-038).

Both LiveExecutor and PaperE
**Classes**: AbstractExecutor

### `src/execution/live.py` (986 lines)
**Purpose**: Live trading executor — real money order placement via ccxt.

Mirrors PaperExecu
**Classes**: LivePosition, ApprovalRequest, LiveExecutor

### `src/execution/paper.py` (883 lines)
**Purpose**: Paper trading executor.

Simulates trade execution against live market prices wi
**Classes**: PaperPosition, ApprovalRequest, PaperExecutor

### `src/features/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/features/pipeline.py` (831 lines)
**Purpose**: Feature engineering pipeline.

Implements every feature from the signal architec
**Classes**: TripleBarrierResult, FeatureMatrix
**Key functions**: fract

### `src/regime/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/regime/detector.py` (650 lines)
**Purpose**: GaussianHMM regime detector — Hamilton (1989) 3-state switching model.

States:

**Classes**: RegimePrediction, RegimeDetector

### `src/risk/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/risk/gates.py` (563 lines)
**Purpose**: Risk gate engine — hard limits that block new positions.

Gates (all must pass f
**Classes**: GateStatus, GateResult, RiskGateContext, DrawdownTracker
**Key functions**: check

### `src/risk/kelly.py` (529 lines)
**Purpose**: Kelly position sizing — half-Kelly with hard ceiling.

Kelly (1956) "A New Inter
**Classes**: KellyResult
**Key functions**: kelly

### `src/strategies/__init__.py` (0 lines)
**Purpose**: __init__ module

### `src/strategies/filters.py` (467 lines)
**Purpose**: Professional strategy filters and signal enrichment.

Implements research-backed
**Key functions**: ewm_t

### `src/strategies/position_sizing.py` (287 lines)
**Purpose**: Advanced position sizing — Carver (2019) and López de Prado (2018).

Implements 
**Key functions**: carve

### `tests/test_features.py` (419 lines)
**Purpose**: Tests for src/features/pipeline.py — all feature functions and the full pipeline
**Classes**: TestFracDiffWeights, TestFractionalDifferentiation, TestVWAPDevZscore, TestOrderFlowImbalance, TestRealizedVolRatio, TestATRMomentum, TestRollingSharpe, TestVolumeZscore, TestComputeDailyVol, TestTripleBarrierLabels, TestMetaLabels, TestBuildFeatureMatrix, TestBuildInferenceFeatures
**Key functions**: reset

### `tests/test_kelly.py` (264 lines)
**Purpose**: Tests for src/risk/kelly.py — Kelly formula, sizing, win/loss stats.
**Classes**: TestKellyFraction, TestHalfKellyFraction, TestKellyFromModelProbs, TestFloorToPrecision, TestSizePosition, TestComputePositionSize, TestComputeWinLossStats
**Key functions**: reset

### `tests/test_risk_gates.py` (274 lines)
**Purpose**: Tests for src/risk/gates.py — all risk gate functions and the full stack.
**Classes**: TestDailyDrawdown, TestConsecutiveLosses, TestRegimeGate, TestPositionSize, TestLiveGate, TestPaperMinimumDays, TestEvaluateAllGates, TestDrawdownTracker
**Key functions**: reset

### `trade-bot-intel-system/final-delivery/daemon/auto_prompt.py` (257 lines)
**Purpose**: Auto Prompt Builder
====================
Wraps your raw message with project con
**Key functions**: find_

### `trade-bot-intel-system/final-delivery/daemon/intel_daemon.py` (280 lines)
**Purpose**: Project Intelligence Daemon
=============================
Runs as a background s
**Classes**: SourceChangeHandler, SessionTracker, IntelExtractor, IntelDaemon
**Key functions**: main

### `trade-bot-intel-system/final-delivery/daemon/output_monitor.py` (205 lines)
**Purpose**: Output Monitor
===============
Watches for agent output and automatically update
**Key functions**: parse

### `trade-bot-intel-system/final-delivery/daemon/output_router.py` (328 lines)
**Purpose**: Output Router
==============
Intercepts agent output and routes it to the right 
**Classes**: RoutedChunk, ProjectFileWriter
**Key functions**: parse

### `trade-bot-intel-system/final-delivery/daemon/primer_template.py` (162 lines)
**Purpose**: CONTEXT_PRIMER builder with Output Routing Protocol embedded.
Agents read this o
**Key functions**: rende

### `trade-bot-intel-system/final-delivery/scripts/extract_intelligence.py` (643 lines)
**Purpose**: Project Intelligence Extractor
================================
Transforms a cod
**Key functions**: extra

### `trade-bot-intel-system/final-delivery/scripts/update_session.py` (106 lines)
**Purpose**: Session State Updater
======================
Agents run this at the END of every
**Key functions**: main

### `trade-bot-intel-system/final-delivery/templates/AGENT_PROMPTS.py` (139 lines)
**Purpose**: agent_prompts module

## Risk Architecture
Gates execute sequentially — first fail short-circuits remaining gates:
1. Daily drawdown halt: 2% of starting equity
2. Consecutive loss halt: 3 trades
3. Regime gate: block when HMM state = volatile
4. Max position size: 5% of capital
5. Paper minimum: 30 days required
6. Live gate: OOS Sharpe > 1.5, max DD < 15%, 500+ trades

## Execution Modes
- AUTOMATIC: fires within risk gates, no approval
- RESTRICTED: auto below notional limit, approval above, 30s timeout skip
- MANUAL: every trade queued for operator approval

## Timeframes
- 1m: scalping, paper only
- 15m: primary real-money intraday
- 4h: swing, paper only

## Key Design Decisions (ADR)
- ADR-001: Triple-barrier + CPCV chosen over simple train/test (eliminates lookahead + serial correlation)
- ADR-002: Meta-labeling separates direction from bet confidence
- ADR-003: Fractional diff d=0.4 balances stationarity and memory preservation
- ADR-004: Half-Kelly at 0.5× with 25% ceiling — Thorp conservative for single-strategy
- ADR-005: SQLite WAL for development; migration path to TimescaleDB for live scale
- ADR-006: Paper mode default — live requires explicit env var + gate pass

## Known Gaps (open architecture items)
- GAP-001: No slippage/market-impact model in live.py (Almgren-Chriss needed)
- GAP-002: HMM regime has no posterior entropy gate (confidence not quantified)
- GAP-003: KS-test drift detection misses label shift (performance-based trigger needed)
- GAP-004: No order state machine (PENDING→FILLED FSM) in live executor
- GAP-005: No portfolio correlation layer for multi-symbol operation
- GAP-006: SQLite write contention under high-frequency multi-timeframe load