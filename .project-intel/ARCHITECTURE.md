# Trade Bot — Architecture Intelligence
> Auto-generated 2026-07-12 17:12 | 178 Python modules | 60,809 total lines

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

### `.project-intel/scripts/agent_detect.py` (77 lines)
**Purpose**: Agent Detector
==============
Detects which agent is currently active from envir
**Key functions**: detec

### `.project-intel/scripts/cognitive_layer.py` (565 lines)
**Purpose**: Cognitive Architecture Layer
==============================
Persistent domain kn
**Key functions**: build

### `.project-intel/scripts/context_builder.py` (319 lines)
**Purpose**: Smart Context Builder
======================
Assembles the MINIMUM context for a
**Key functions**: find_

### `.project-intel/scripts/extract_intelligence.py` (669 lines)
**Purpose**: Project Intelligence Extractor
================================
Transforms a cod
**Key functions**: extra

### `.project-intel/scripts/handoff.py` (358 lines)
**Purpose**: Agent Handoff Manager
======================
Tracks which agent is working, what
**Key functions**: cmd_s

### `.project-intel/scripts/rag_engine.py` (386 lines)
**Purpose**: RAG Engine — BM25 on SQLite, zero external dependencies
========================
**Classes**: BM25Index
**Key functions**: token

### `.project-intel/scripts/resume.py` (121 lines)
**Purpose**: SESSION RESUME — single command, complete context, zero follow-up reads.
Output
**Key functions**: git_s

### `.project-intel/scripts/smart_read.py` (62 lines)
**Purpose**: Compact file-reading wrapper for agents.

This script is the preferred way to in
**Key functions**: build

### `.project-intel/scripts/update_session.py` (106 lines)
**Purpose**: Session State Updater
======================
Agents run this at the END of every
**Key functions**: main

### `scripts/backfill_intelligence.py` (306 lines)
**Purpose**: GAP-015 Step 3 — Historical intelligence features backfill (OCI-012 revision).



### `scripts/check_coverage_floors.py` (169 lines)
**Purpose**: Per-package / per-file coverage floor enforcement — GAP-020.

Reads the .coverag
**Key functions**: main

### `scripts/claude_debug_analysis.py` (118 lines)
**Purpose**: scripts/claude_debug_analysis.py — Called by auto-debug.yml GitHub Action.

Read
**Key functions**: main

### `scripts/export_diagnostics.py` (154 lines)
**Purpose**: Run ruff + pyright (+ eslint if frontend/ exists) and write DIAGNOSTICS.md
at pr
**Key functions**: main

### `scripts/extract_tagpack_seeds.py` (147 lines)
**Purpose**: GAP-015 on-chain pipeline, phase 1: extract exchange/miner seed addresses.

Sour
**Key functions**: extra

### `scripts/migrate_sqlite_to_timescale.py` (160 lines)
**Purpose**: GAP-006: one-shot data migration — SQLite -> local TimescaleDB.

Copies every ro
**Key functions**: main

### `scripts/run_tuning_attempt.py` (308 lines)
**Purpose**: Manual operator entrypoint for one self-tuning attempt cycle.

Design: docs/SELF
**Key functions**: main

### `scripts/smart_read.py` (15 lines)
**Purpose**: Compatibility wrapper for the compact file reader.

### `src/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/api/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/api/auth.py` (98 lines)
**Purpose**: API authentication — API key validation for REST and WebSocket.

All routes and
**Key functions**: verif

### `src/api/main.py` (1370 lines)
**Purpose**: FastAPI dashboard API.

Security: ALL endpoints require X-API-Key header matchin
**Classes**: AppState, ResolveApprovalRequest, SetExecutionModeRequest, SetRiskControlsRequest, SelfTuningPauseRequest, SelfTuningRollbackRequest
**Key functions**: api_k

### `src/api/metrics.py` (187 lines)
**Purpose**: Prometheus metrics for Trade Bot — TASK-007.

Exposes GET /metrics in Prometheus
**Key functions**: updat

### `src/api/middleware.py` (52 lines)
**Purpose**: CORS validation middleware — prevents wildcard + credentials misconfiguration
an
**Key functions**: valid

### `src/config.py` (809 lines)
**Purpose**: Production configuration for the algorithmic trading bot.

Authority sources:

**Classes**: TradingMode, ExecutionMode, Timeframe, BinanceSettings, OKXSettings, RiskSettings, HMMSettings, XGBoostSettings, FeatureSettings, StorageSettings, APISettings, IntelligenceSettings, SelfTuningSettings, Settings, RuntimeConfig
**Key functions**: get_s

### `src/data/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/data/fetcher.py` (763 lines)
**Purpose**: Async market data fetcher — Binance (primary) + OKX (secondary).

Responsibiliti
**Classes**: OrderBookSnapshot, MarketDataFetcher, _FetcherContextManager
**Key functions**: open_

### `src/data/storage.py` (1834 lines)
**Purpose**: Async SQLite storage layer — aiosqlite, WAL mode, typed queries.

Schema owns fi
**Classes**: BarRecord, TradeRecord, MissedTradeRecord, RegimeSnapshotRecord, ModelMetricsRecord, EquityRecord, StorageBackend
**Key functions**: creat

### `src/data/timescale_storage.py` (1439 lines)
**Purpose**: GAP-006: Async TimescaleDB storage backend — asyncpg pool, hypertables, typed qu
**Classes**: TimescaleBackend

### `src/diagnostics/__init__.py` (0 lines)
**Purpose**: __init__ module

### `src/diagnostics/runtime_monitor.py` (316 lines)
**Purpose**: Runtime Monitor — continuous async health diagnostics (alert-only, no auto-resta
**Classes**: ProbeResult, HealthSnapshot, RuntimeMonitor
**Key functions**: get_m

### `src/diagnostics/signal_debugger.py` (424 lines)
**Purpose**: Signal Debugger — feature drift detection, model degradation scanner,

**Classes**: FeatureDriftRecord, FeatureDriftMonitor, PredictionRecord, ModelDegradationTracker, LabelShiftRecord, LabelShiftDetector
**Key functions**: run_p

### `src/diagnostics/trade_auditor.py` (281 lines)
**Purpose**: Trade Auditor — captures every signal decision with full diagnostic context.

Ev
**Classes**: AuditRecord, TradeAuditor
**Key functions**: get_a

### `src/engine/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/engine/orchestrator.py` (884 lines)
**Purpose**: Orchestrator — top-level async event loop coordinating all subsystems.

Responsi
**Classes**: Orchestrator

### `src/engine/signal_engine.py` (831 lines)
**Purpose**: Signal engine — per-timeframe signal computation pipeline.

On every tick for a
**Classes**: SignalResult, SignalEngine

### `src/execution/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/execution/base.py` (118 lines)
**Purpose**: Abstract base class for trade executors (VUL-038).

Both LiveExecutor and PaperE
**Classes**: AbstractExecutor

### `src/execution/live.py` (1047 lines)
**Purpose**: Live trading executor — real money order placement via ccxt.

Mirrors PaperExecu
**Classes**: LivePosition, ApprovalRequest, LiveExecutor

### `src/execution/live_fsm_integration.py` (105 lines)
**Purpose**: Live Executor FSM Integration — refactored order placement with OrderFSM.

Repla
**Classes**: LiveExecutorOrderFSM

### `src/execution/order_fsm.py` (300 lines)
**Purpose**: Order Finite State Machine — formalized order lifecycle with state transitions.

**Classes**: OrderStatus, OrderFSMError, OrderFSMState, OrderFSM

### `src/execution/order_manager.py` (289 lines)
**Purpose**: Order Manager — FSM-based order lifecycle management for live executor.

Wraps c
**Classes**: OrderManager

### `src/execution/paper.py` (929 lines)
**Purpose**: Paper trading executor.

Simulates trade execution against live market prices wi
**Classes**: PaperPosition, ApprovalRequest, PaperExecutor

### `src/features/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/features/intelligence_features.py` (167 lines)
**Purpose**: Intelligence-augmented features.

Extends core feature pipeline (9 features) wit
**Classes**: IntelligenceFeatureMatrix
**Key functions**: add_i

### `src/features/pipeline.py` (1002 lines)
**Purpose**: Feature engineering pipeline.

Implements every feature from the signal architec
**Classes**: TripleBarrierResult, FeatureMatrix
**Key functions**: get_a

### `src/intelligence/__init__.py` (19 lines)
**Purpose**: Crypto intelligence layer — on-chain metrics, exchange flows, whale tracking.

P

### `src/intelligence/calibration.py` (100 lines)
**Purpose**: Shared calibration and Bayesian-shrinkage primitives.

Extracted from the Beta-c
**Key functions**: shrin

### `src/intelligence/causal_inference.py` (469 lines)
**Purpose**: EXPERIMENTAL — NOT wired into live signal path.
Blocked on API key provisioning
**Classes**: CausalEffect, CausalDAG, CausalInferenceEngine

### `src/intelligence/client.py` (674 lines)
**Purpose**: Multi-provider intelligence client aggregator.

Responsibilities:
  - Manage cre
**Classes**: CacheEntry, IntelligenceAggregator
**Key functions**: get_i

### `src/intelligence/ensemble_predictor.py` (658 lines)
**Purpose**: EXPERIMENTAL — NOT wired into live signal path.
Blocked on API key provisioning
**Classes**: EnsemblePrediction, PredictionModel, ARIMAPredictor, XGBoostPredictor, LSTMPredictor, GaussianProcessPredictor, TreeEnsemblePredictor, EnsemblePredictor, _LSTMNet

### `src/intelligence/metrics.py` (298 lines)
**Purpose**: Intelligence metrics computation layer.

Transforms raw provider data into tradi
**Classes**: IntelligenceMetrics, IntelligenceAnalyzer

### `src/intelligence/onchain/__init__.py` (53 lines)
**Purpose**: On-chain intelligence providers.

OCI-001: Foundation layer — RateLimiter, Circu

### `src/intelligence/onchain/arkham_provider.py` (205 lines)
**Purpose**: Arkham Intel provider — OCI-002.

Populates IntelligenceMetrics fields:
  exchan
**Classes**: ArkhamProvider

### `src/intelligence/onchain/base.py` (240 lines)
**Purpose**: On-chain provider foundation: RateLimiter, CircuitBreaker, AsyncHTTPCache, OnCha
**Classes**: CircuitOpenError, RateLimiter, _CBState, CircuitBreaker, AsyncHTTPCache, OnChainProvider

### `src/intelligence/onchain/coinglass_provider.py` (273 lines)
**Purpose**: Coinglass provider — OCI-006.

Supplements exchange providers with open-interest
**Classes**: CoinglassProvider

### `src/intelligence/onchain/cryptoquant_provider.py` (240 lines)
**Purpose**: CryptoQuant provider — OCI-005.

Supplements Arkham (exchange flows) and Dune (m
**Classes**: CryptoQuantProvider

### `src/intelligence/onchain/defillama_provider.py` (150 lines)
**Purpose**: DeFiLlama provider — OCI-003.

Populates:
  staking_unlock_risk       — proxy vi
**Classes**: DeFiLlamaProvider

### `src/intelligence/onchain/dune_provider.py` (226 lines)
**Purpose**: Dune Analytics provider — OCI-004.

CACHE-FIRST design: never execute a query un
**Classes**: DuneProvider

### `src/intelligence/onchain/schema.py` (200 lines)
**Purpose**: OCI-007 — Canonical on-chain metrics schema.

Defines the unified dict structure
**Key functions**: valid

### `src/intelligence/probabilistic.py` (459 lines)
**Purpose**: Probabilistic inference engine for crypto intelligence.

Replaces deterministic
**Classes**: ProbabilisticPrediction, RiskAssessment, BayesianExchangeStressModel, BayesianWhaleActivityModel, BayesianRegimeDetection

### `src/intelligence/probabilistic_adapter.py` (200 lines)
**Purpose**: ProbabilisticMetricsAdapter
===========================
Wraps BinanceIntelligenc
**Classes**: ProbabilisticGateInputs, ProbabilisticMetricsAdapter

### `src/intelligence/providers/__init__.py` (10 lines)
**Purpose**: Crypto intelligence providers.

Each provider wraps a specific API:
  - glassnod

### `src/intelligence/providers/aggregator.py` (477 lines)
**Purpose**: Multi-provider intelligence aggregator.

Merges outputs from all configured Exch
**Classes**: MultiProviderIntelligenceAggregator, OnChainAwareAggregator
**Key functions**: get_m

### `src/intelligence/providers/base.py` (65 lines)
**Purpose**: Abstract base for exchange intelligence providers.

Every exchange-specific prov
**Classes**: ExchangeIntelligenceProvider

### `src/intelligence/providers/binance_provider.py` (484 lines)
**Purpose**: Binance public REST provider for intelligence metrics.

All endpoints used here
**Classes**: BinanceIntelligenceProvider
**Key functions**: get_b

### `src/intelligence/providers/blockchain_provider.py` (200 lines)
**Purpose**: Blockchain.info free public REST provider for network activity metrics.

Covers
**Classes**: BlockchainIntelligenceProvider
**Key functions**: get_b

### `src/intelligence/providers/coingecko_provider.py` (195 lines)
**Purpose**: CoinGecko free public REST provider for macro intelligence metrics.

Covers cros
**Classes**: CoinGeckoIntelligenceProvider
**Key functions**: get_c

### `src/intelligence/providers/okx_provider.py` (389 lines)
**Purpose**: OKX public REST provider for intelligence metrics.

All endpoints used here are
**Classes**: OKXIntelligenceProvider
**Key functions**: get_o

### `src/intelligence/risk_quantification.py` (416 lines)
**Purpose**: EXPERIMENTAL — NOT wired into live signal path.
Blocked on API key provisioning
**Classes**: RiskMetrics, RiskQuantifier

### `src/regime/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/regime/detector.py` (709 lines)
**Purpose**: GaussianHMM regime detector — Hamilton (1989) 3-state switching model.

States:

**Classes**: RegimePrediction, RegimeDetector

### `src/risk/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/risk/cognitive_engine.py` (807 lines)
**Purpose**: Cognitive Engine — Mandatory Runtime Decision Layer
============================
**Classes**: ValidatorStatus, ValidatorResult, CognitiveDecision, SignalContext, Validator, QuantValidator, ProbabilityValidator, RiskValidator, BlockchainValidator, RegimeValidator, CognitiveEngine
**Key functions**: get_c

### `src/risk/drift_integration.py` (120 lines)
**Purpose**: Drift Detector Integration — hooks for orchestrator to record trade outcomes.

A
**Classes**: DriftIntegrationAdapter

### `src/risk/gates.py` (938 lines)
**Purpose**: Risk gate engine — hard limits that block new positions.

Gates (all must pass f
**Classes**: GateStatus, GateResult, RiskGateContext, DrawdownTracker
**Key functions**: check

### `src/risk/kelly.py` (667 lines)
**Purpose**: Kelly position sizing — half-Kelly with hard ceiling.

Kelly (1956) "A New Inter
**Classes**: KellyResult
**Key functions**: kelly

### `src/risk/performance_drift.py` (414 lines)
**Purpose**: Performance Drift Trigger — detects model decay in live trading.

Monitors:
  1.
**Classes**: PerformanceBaseline, DriftDetected, PerformanceDriftDetector

### `src/risk/portfolio_correlation.py` (326 lines)
**Purpose**: Portfolio Correlation Layer — Gap-005.

Tracks rolling pairwise return correlati
**Classes**: _EWMSeries, _EWMCov, PortfolioCorrelationTracker
**Key functions**: get_p

### `src/risk/slippage.py` (229 lines)
**Purpose**: Slippage and market-impact model — Almgren-Chriss square-root impact.

GAP-001:
**Classes**: SlippageEstimate, SlippageModel

### `src/strategies/__init__.py` (0 lines)
**Purpose**: __init__ module

### `src/strategies/filters.py` (482 lines)
**Purpose**: Professional strategy filters and signal enrichment.

Implements research-backed
**Key functions**: ewm_t

### `src/strategies/position_sizing.py` (312 lines)
**Purpose**: Advanced position sizing — Carver (2019) and López de Prado (2018).

Implements
**Key functions**: carve

### `src/tuning/__init__.py` (9 lines)
**Purpose**: Self-tuning subsystem — see docs/SELF_TUNING_DESIGN.md.

Registration (src/tunin

### `src/tuning/audit.py` (111 lines)
**Purpose**: Immutable audit trail for self-tuning attempts.

Design: docs/SELF_TUNING_DESIGN
**Classes**: TuningEventType, TuningAuditEntry, TuningAuditLog

### `src/tuning/backtest_harness.py` (538 lines)
**Purpose**: Backtest harness — produces real MetricComparison samples for the
self-tuning ev
**Classes**: TradeSample, InsufficientDataError, SlippageFillSample, UnknownFeatureWindowFieldError, UnknownXGBHyperparamFieldError
**Key functions**: run_e

### `src/tuning/bootstrap.py` (280 lines)
**Purpose**: Explicit parameter registration for the self-tuning subsystem.

Design: docs/SEL
**Key functions**: regis

### `src/tuning/evaluator.py` (239 lines)
**Purpose**: Champion-vs-challenger statistical evaluator for the self-tuning subsystem.

Des
**Classes**: InsufficientSampleError, MetricComparison, EvaluationResult, ChallengerEvaluator
**Key functions**: proba

### `src/tuning/gate.py` (71 lines)
**Purpose**: Promotion gate for the self-tuning subsystem.

Design: docs/SELF_TUNING_DESIGN.m
**Classes**: GateDecision, PromotionGate

### `src/tuning/live_overrides.py` (121 lines)
**Purpose**: Live-value overlay for self-tuned parameters.

Design: docs/SELF_TUNING_DESIGN.m
**Key functions**: effec

### `src/tuning/proposer.py` (60 lines)
**Purpose**: Candidate-value proposer for the self-tuning subsystem.

Design: docs/SELF_TUNIN
**Classes**: Proposal, TuningProposer

### `src/tuning/registry.py` (169 lines)
**Purpose**: Parameter registry for the self-tuning subsystem.

Design: docs/SELF_TUNING_DESI
**Classes**: ExcludedParameterError, InvalidBoundsError, DuplicateParameterError, UnknownParameterError, TunableParameter, ParameterRegistry

### `src/tuning/runner.py` (210 lines)
**Purpose**: Shadow-mode tuning runner -- ties proposer + evaluator + gate + store +
audit lo
**Classes**: AttemptResult, SelfTuningDisabledError, TuningRunner

### `src/tuning/scheduler.py` (480 lines)
**Purpose**: Auto-tuning scheduler -- the "explicit startup step" referenced by
src/tuning/bo
**Classes**: AutoTuningScheduler

### `src/tuning/state.py` (72 lines)
**Purpose**: Process-wide singletons for the self-tuning subsystem.

Mirrors the pattern of `
**Classes**: _PauseState

### `src/tuning/store.py` (167 lines)
**Purpose**: Versioned, append-only config store for the self-tuning subsystem.

Design: docs
**Classes**: NoVersionsError, NoPriorVersionError, ConfigVersion, VersionedConfigStore

### `src/tuning/watchdog.py` (140 lines)
**Purpose**: Post-promotion watchdog -- auto-rollback on live drift.

Design: docs/SELF_TUNIN
**Classes**: WatchdogOutcome, _ProbationState, PostPromotionWatchdog

### `tests/intelligence/__init__.py` (0 lines)
**Purpose**: __init__ module

### `tests/intelligence/onchain/__init__.py` (0 lines)
**Purpose**: __init__ module

### `tests/intelligence/onchain/test_arkham_provider.py` (215 lines)
**Purpose**: OCI-002: Tests for ArkhamProvider.
All HTTP calls mocked via unittest.mock.Async
**Key functions**: test_

### `tests/intelligence/onchain/test_base.py` (148 lines)
**Purpose**: OCI-001: Unit tests — RateLimiter, CircuitBreaker, AsyncHTTPCache.

### `tests/intelligence/onchain/test_coinglass_provider.py` (234 lines)
**Purpose**: OCI-006 — CoinglassProvider unit tests.
All HTTP calls are mocked; no network re
**Classes**: TestExtractList, TestOiChangePct, TestLiqZscore, TestHeatmapMax, TestExtractFunding, TestLsRatio, TestCoinglassProviderDisabled, TestCoinglassProviderEnabled

### `tests/intelligence/onchain/test_cryptoquant_provider.py` (273 lines)
**Purpose**: OCI-005 — CryptoQuantProvider unit tests.
All HTTP calls are mocked; no network
**Classes**: TestExtractRows, TestReserveRatio, TestNetflowZscore, TestMinerSignal, TestExtractBinanceFunding, TestMvrvStressContrib, TestCryptoQuantProviderDisabled, TestCryptoQuantProviderEnabled

### `tests/intelligence/onchain/test_defillama_provider.py` (166 lines)
**Purpose**: OCI-003: Tests for DeFiLlamaProvider.
**Key functions**: test_

### `tests/intelligence/onchain/test_dune_provider.py` (260 lines)
**Purpose**: OCI-004: Tests for DuneProvider.
**Key functions**: test_

### `tests/intelligence/onchain/test_onchain_aggregator_integration.py` (208 lines)
**Purpose**: OCI-009 — OnChainAwareAggregator integration tests.

Verifies the full blending
**Classes**: TestOnChainAwareAggregatorNoOnChain, TestOnChainAwareAggregatorBlending

### `tests/intelligence/onchain/test_onchain_gating.py` (118 lines)
**Purpose**: OCI-010 — On-chain field gating policy tests.

Verifies that GATED_FIELDS remain
**Classes**: TestGatedFieldsDefinition, TestGatingViaDisabledProviders, TestMergeGatingPolicy

### `tests/intelligence/onchain/test_schema.py` (174 lines)
**Purpose**: OCI-007 — on-chain schema validation and merge tests.
**Classes**: TestONCHAIN_NEUTRAL, TestValidateProviderResult, TestMergeOnchainResults, TestNewSchemaFields

### `tests/test_api_and_fsm_coverage.py` (402 lines)
**Purpose**: Coverage for small zero-coverage modules — Debt-005.

Covers:
  - src/api/middle
**Classes**: TestValidateCorsConfig, TestGetConfiguredKey, TestVerifyApiKey, TestVerifyWsKey, TestLiveExecutorOrderFSMInit, TestLiveExecutorOrderFSMPlaceOrder, TestIntelligenceEndpoints

### `tests/test_api_main_coverage.py` (975 lines)
**Purpose**: Tests for src/api/main.py — target 70%+ coverage.
**Classes**: TestAppState, _FetcherCtx, _FetcherCtx, _FetcherCtx, _FetcherCtx, _FetcherCtx, _NoFsmExecutor
**Key functions**: test_

### `tests/test_calibration.py` (114 lines)
**Purpose**: Tests for src/intelligence/calibration.py.
**Classes**: TestShrinkProbability, TestBrierScore, TestCoverageFrequency

### `tests/test_causal_inference.py` (244 lines)
**Purpose**: Tests for src/intelligence/causal_inference.py (0% → target 85%+).
**Classes**: TestCausalDAG, TestCausalEffect, TestCausalInferenceEngine

### `tests/test_cognitive_engine.py` (429 lines)
**Purpose**: Tests for src/risk/cognitive_engine.py — mandatory five-validator decision
layer
**Classes**: TestQuantValidator, TestProbabilityValidator, TestRiskValidator, TestBlockchainValidator, TestRegimeValidator, TestCognitiveEngineAggregation
**Key functions**: reset

### `tests/test_config_feature_settings.py` (28 lines)
**Purpose**: Tests for FeatureSettings.validate_purge_gap_covers_label_horizon (UI-005).
**Key functions**: test_

### `tests/test_context_builder.py` (37 lines)
**Purpose**: test_context_builder module
**Key functions**: load_

### `tests/test_coverage_boost.py` (446 lines)
**Purpose**: Coverage boost: intelligence/metrics, probabilistic_adapter, coingecko_provider,
**Classes**: TestIntelligenceAnalyzer, TestProbabilisticMetricsAdapter, TestCoinGeckoProvider
**Key functions**: api_c

### `tests/test_coverage_boost2.py` (384 lines)
**Purpose**: Coverage boost 2: blockchain_provider, order_manager, performance_drift.
**Classes**: TestBlockchainProvider, TestOrderManager, TestPerformanceDrift

### `tests/test_coverage_boost3.py` (710 lines)
**Purpose**: Coverage boost 3: api/main more routes, okx_provider, strategies/filters.
**Classes**: TestOKXProvider, TestStrategyFilters
**Key functions**: clien

### `tests/test_coverage_boost4.py` (293 lines)
**Purpose**: Coverage boost 4: ensemble_predictor, live.py place paths, api/main remaining.
**Classes**: TestARIMAPredictor, TestXGBoostPredictor, TestGaussianProcessPredictor, TestTreeEnsemblePredictor, TestEnsemblePredictor, TestLiveExecutorPlacePaths

### `tests/test_coverage_boost5.py` (566 lines)
**Purpose**: Coverage boost 5: binance_provider, risk/gates uncovered paths.
**Classes**: TestBinanceProvider, TestGatesUncoveredPaths

### `tests/test_coverage_boost6.py` (197 lines)
**Purpose**: Coverage boost 6: DrawdownTracker edge cases, exchange stress medium path,
perfo
**Classes**: TestDrawdownTrackerEdgeCases, TestExchangeStressMediumPath, TestCheckPerformanceDrift, TestCoinGeckoZscorePath, TestBlockchainEdgeCases

### `tests/test_detector.py` (412 lines)
**Purpose**: Tests for src/regime/detector.py — GaussianHMM 3-state regime detector
(Hamilton
**Classes**: TestRegimePredictionEntropyMath, TestPositionScalar, TestRegimeDetectorFit, TestRegimeDetectorPredict, TestRegimeDetectorPersistence, TestRegimeStatistics
**Key functions**: reset

### `tests/test_drift_integration_coverage.py` (121 lines)
**Purpose**: Coverage for src/risk/drift_integration.py — Debt-005.
**Classes**: TestDriftIntegrationAdapterInit, TestRecordClosedTrade, TestCheckDrift

### `tests/test_ensemble_predictor.py` (491 lines)
**Purpose**: Tests for src/intelligence/ensemble_predictor.py (0% → target 75%+).
**Classes**: TestEnsemblePrediction, TestARIMAPredictor, TestXGBoostPredictor, TestLSTMPredictor, TestGaussianProcessPredictor, TestTreeEnsemblePredictor, TestEnsemblePredictor

### `tests/test_features.py` (445 lines)
**Purpose**: Tests for src/features/pipeline.py — all feature functions and the full pipeline
**Classes**: TestFracDiffWeights, TestFractionalDifferentiation, TestVWAPDevZscore, TestOrderFlowImbalance, TestRealizedVolRatio, TestATRMomentum, TestRollingSharpe, TestVolumeZscore, TestComputeDailyVol, TestTripleBarrierLabels, TestMetaLabels, TestBuildFeatureMatrix, TestBuildInferenceFeatures
**Key functions**: reset

### `tests/test_fetcher_coverage.py` (768 lines)
**Purpose**: Tests for src/data/fetcher.py — target 80%+ coverage.
**Key functions**: test_

### `tests/test_gap015_backfill.py` (380 lines)
**Purpose**: GAP-015 backfill pipeline tests.

Covers:
  - storage migration v3 creates intel
**Classes**: _FakeSettings, _FakeSettingsWithKey
**Key functions**: tmp_d

### `tests/test_integration_full_pipeline.py` (291 lines)
**Purpose**: End-to-end integration tests for full trading pipeline.

Tests:
  - Order placem
**Classes**: TestDriftGateIntegration, TestDriftIntegrationAdapter, TestOrderFSMInContext, TestOrderFSMStateSnapshot

### `tests/test_intelligence_client_coverage.py` (723 lines)
**Purpose**: Tests for src/intelligence/client.py — target 85%+ coverage.
**Key functions**: test_

### `tests/test_intelligence_features_coverage.py` (194 lines)
**Purpose**: Tests for src/features/intelligence_features.py.
**Key functions**: test_

### `tests/test_intelligence_metrics.py` (213 lines)
**Purpose**: Regression tests for two bugs found and fixed this session (GAP-015 follow-on):

**Classes**: TestWhaleTakerRatioFix, TestComputeMetricsConfidenceFix

### `tests/test_intelligence_providers.py` (843 lines)
**Purpose**: Tests for multi-provider intelligence wiring (GAP-015).

Coverage:
  1. Exchange
**Classes**: TestExchangeIntelligenceProviderABC, TestOKXIntelligenceProvider, TestCoinGeckoIntelligenceProvider, TestBlockchainIntelligenceProvider, TestMultiProviderIntelligenceAggregator, TestInjectIntelligenceFeatures, TestBuildInferenceFeaturesWithIntelligence, TestGetMultiProviderAggregatorSingleton, TestGetOnChainAwareAggregatorSingleton, Incomplete, Complete, MockProvider
**Key functions**: base_

### `tests/test_kelly.py` (759 lines)
**Purpose**: Tests for src/risk/kelly.py — Kelly formula, sizing, win/loss stats.
**Classes**: TestKellyFraction, TestHalfKellyFraction, TestKellyFromModelProbs, TestFloorToPrecision, TestSizePosition, TestComputePositionSize, TestComputeWinLossStats, TestUncertaintyScalar
**Key functions**: reset

### `tests/test_kelly_gaps.py` (308 lines)
**Purpose**: Targeted tests closing remaining coverage gaps in src/risk/kelly.py.

Companion
**Classes**: TestKellyResultPositionSizePct, TestHalfKellyFractionBoundsValidation, TestKellyFromModelProbsInvalidDirection, TestKellyFromModelProbsNonFinitePLong, TestKellyFromModelProbsNonFiniteWinLossRatio, TestSizePositionMinAmountRejection, TestSizePositionMaxPositionPctValidation, TestComputePositionSizeDefaultCfg, TestComputeWinLossStatsAllWinsOrAllLosses
**Key functions**: cfg

### `tests/test_live_additional_coverage.py` (395 lines)
**Purpose**: Additional coverage for src/execution/live.py uncovered paths.
**Key functions**: test_

### `tests/test_live_executor_coverage.py` (888 lines)
**Purpose**: Coverage tests for LiveExecutor critical paths (Debt-009).

Uses object.__new__
**Classes**: TestInitialize, TestProperties, TestSubmitSignalRouting, TestPlaceAndRecordGuards, TestMarkToMarket, TestClosePosition, TestEquityAccounting, TestInit, TestSubmitSignalAuto, TestEnqueueApproval, TestPendingApprovalsSafe, TestAwaitApproval, TestSubmitSignalWithApproval, TestPlaceMarketOrder, TestOrderFsmRegistry, TestExtractFee, TestRequireInitialized

### `tests/test_live_executor_fsm.py` (276 lines)
**Purpose**: Integration tests for LiveExecutor with OrderFSM.

Tests that _place_market_orde
**Classes**: TestOrderManagerMock, TestFSMStateTransitions, TestOrderReconciliation

### `tests/test_metrics.py` (120 lines)
**Purpose**: Tests for src/api/metrics.py — TASK-007 Prometheus endpoint.
**Classes**: TestUpdateMetrics, TestMetricsOutput

### `tests/test_model_trainer_coverage.py` (923 lines)
**Purpose**: Coverage for src/models/trainer.py — Debt-005.

Targets predict_direction, predi
**Classes**: TestModelTrainerInit, TestPredictDirection, TestPredictMeta, TestComputeWinLossStats, TestTrainingResult, TestAtomicWriteBytes, TestManifest, TestBuildGroups, TestApplyPurgeEmbargo, TestBuildCPCVFolds, TestComputeSampleWeights, TestOosSharpeAndDrawdown, TestSaveLoad, TestCheckLiveGate, TestTrainDirection, TestTrainMetaLabel, TestPredictMetaEdgeCases
**Key functions**: test_

### `tests/test_onchain_base_coverage.py` (314 lines)
**Purpose**: Tests for src/intelligence/onchain/base.py.
**Classes**: TestAsyncHTTPCache, TestCircuitBreaker, TestRateLimiter, ConcreteProvider, TestOnChainProvider

### `tests/test_online_trainer.py` (209 lines)
**Purpose**: Tests for src/models/online_trainer.py — TASK-008.

Covers: warmup gating, blend
**Key functions**: test_

### `tests/test_orchestrator.py` (209 lines)
**Purpose**: Tests for src/engine/orchestrator.py

Focus: correlation scalar computation (GAP
**Classes**: TestPortfolioCorrelationTracker, TestOrchestratorCorrelationState, TestCorrelationScalarFailSafe

### `tests/test_orchestrator_coverage.py` (1017 lines)
**Purpose**: Comprehensive coverage tests for src/engine/orchestrator.py.

Coverage target: 1
**Classes**: TestOrchestratorInit, TestOrchestratorStartup, TestOrchestratorStopShutdown, TestOrchestratorTick, TestOrchestratorTrainModels, TestSleepUntilNextBar, TestMidnightResetLoop, TestPositionMonitorLoop, TestTickCorrelationFallback

### `tests/test_orchestrator_extra_coverage.py` (1247 lines)
**Purpose**: Additional orchestrator coverage targeting uncovered paths.
**Classes**: TestOrchestratorRun, TestTimeframeLoop, TestTickUncoveredBranches, TestStartupLiveModeAndDriftDetector, TestTimeframeLoopRunningFlipsDuringSleep, TestTickUpdateMetricsFailure, TestPositionMonitorDriftRecordOnClose, TestTickScheduledRetrain, TestTrainModelsRemainingBranches, TestPositionMonitorRemainingBranches, TestTimeframeLoopSuccessfulTick

### `tests/test_order_fsm.py` (296 lines)
**Purpose**: Test suite for Order FSM state machine.
**Classes**: TestOrderFSMBasics, TestOrderFSMTransitions, TestPartialFills, TestRetryCounter

### `tests/test_order_fsm_registry.py` (191 lines)
**Purpose**: Tests for the order FSM registry follow-up to GAP-004, and the two
endpoints (GE
**Classes**: TestOrderFSMRegistry, TestOrderStatusEndpoint, TestPerformanceDriftEndpoint, _FakeExecutor, _FakeDriftAdapter, _FakeOrchestrator
**Key functions**: api_c

### `tests/test_order_manager.py` (67 lines)
**Purpose**: Tests for src/execution/order_manager.py — OrderManager fill confirmation.
**Classes**: TestConfirmOrderFill

### `tests/test_paper_executor.py` (583 lines)
**Purpose**: Test coverage for src/execution/paper.py — paper trading executor.
**Classes**: TestPaperPosition, TestApprovalRequestToDict, TestLifecycle, TestSubmitSignalAutomatic, TestSubmitSignalRestricted, TestSubmitSignalManual, TestClosePosition, TestMarkToMarket, TestApprovalQueueManagement, TestApprovalTimeout, TestStateQueriesAndProperties
**Key functions**: make_

### `tests/test_performance_drift.py` (317 lines)
**Purpose**: Test suite for Performance Drift Detector.
**Classes**: TestPerformanceBaseline, TestDriftDetector, TestSharpeDrift, TestAccuracyDrift, TestLiveMetrics, TestSignificanceGatedDrift, TestModelDegradationTracker

### `tests/test_portfolio_correlation.py` (424 lines)
**Purpose**: Tests for src/risk/portfolio_correlation.py

Covers: _EWMSeries, _EWMCov, Portfo
**Classes**: TestEWMSeries, TestEWMCov, TestPushBarReturns, TestPushReturn, TestCorrelation, TestAvgCorrelation, TestCorrelationScalar, TestCorrelationMatrix, TestSingleton

### `tests/test_position_sizing.py` (567 lines)
**Purpose**: Test coverage for src/strategies/position_sizing.py — Carver/AFML/Thorp sizing.
**Classes**: TestCarverForecastPosition, TestVolTargetQuantity, TestEstimateDailyVol, TestCorrelationAdjustedNotional, TestAfmlBetSize, TestThorpKellyWithVariance, TestRecommendPositionNotional

### `tests/test_probabilistic_engine.py` (318 lines)
**Purpose**: Tests for src/intelligence/probabilistic.py (40% → target 85%+).
**Classes**: TestProbabilisticPrediction, TestBayesianExchangeStressModel, TestBayesianWhaleActivityModel, TestBayesianRegimeDetection
**Key functions**: test_

### `tests/test_risk_controls_api.py` (380 lines)
**Purpose**: Tests for GAP-013: runtime-toggleable position-exit controls.

Covers:
  - check
**Classes**: TestCheckPositionExit, TestRuntimeConfigRiskControls, TestRiskControlsEndpoints, _FakeStorage
**Key functions**: api_c

### `tests/test_risk_gates.py` (331 lines)
**Purpose**: Tests for src/risk/gates.py — all risk gate functions and the full stack.
**Classes**: TestDailyDrawdown, TestConsecutiveLosses, TestRegimeGate, TestPositionSize, TestLiveGate, TestPaperMinimumDays, TestEvaluateAllGates, TestDrawdownTracker, TestSlippageVetoGate, TestEvaluateAllGatesSlippageWiring
**Key functions**: reset

### `tests/test_risk_gates_coverage.py` (255 lines)
**Purpose**: Additional coverage for src/risk/gates.py — Debt-005.

Tests individual gate fun
**Classes**: TestSlippageVeto, TestDailyDrawdown, TestConsecutiveLosses, TestRegimeGate, TestPositionSize, TestGateResult, TestEvaluateAllGates

### `tests/test_risk_quantification.py` (278 lines)
**Purpose**: Tests for src/intelligence/risk_quantification.py (0% → target 85%+).
**Classes**: TestRiskMetrics, TestValueAtRisk, TestStressTest, TestProbabilityOfRuin, TestUncertaintyDecomposition, TestPrivateMethods

### `tests/test_runtime_monitor_coverage.py` (278 lines)
**Purpose**: Tests for src/diagnostics/runtime_monitor.py (27% → target 80%+).
**Key functions**: test_

### `tests/test_self_tuning_api.py` (170 lines)
**Purpose**: Tests for Phase 6: GET/POST /self-tuning/* API endpoints.

Follows the same Test
**Classes**: TestSelfTuningStatus, TestSelfTuningPauseResume, TestSelfTuningRollback, _FakeStorage
**Key functions**: api_c

### `tests/test_self_tuning_settings.py` (30 lines)
**Purpose**: Phase 7: verifies the live-promotion capability's default posture and
that it is
**Key functions**: test_

### `tests/test_signal_debugger_coverage.py` (336 lines)
**Purpose**: Tests for src/diagnostics/signal_debugger.py (60% → target 85%+).
**Classes**: TestFeatureDriftMonitor, TestModelDegradationTracker, TestLabelShiftDetector
**Key functions**: test_

### `tests/test_signal_engine.py` (1089 lines)
**Purpose**: Test coverage for src/engine/signal_engine.py — Debt-005.

Strategy: mock all ex
**Classes**: TestSkipShape, TestSkipPaths, TestTradeablePath, TestMtfTrendConfirmation, TestModelSwap, TestTask010FundingRateWiring, TestLoadBars

### `tests/test_slippage.py` (183 lines)
**Purpose**: Tests for src/risk/slippage.py — Almgren-Chriss slippage/impact model.
**Classes**: TestEstimate, TestVetoIfNegativeEv, TestSelfTuningLiveOverride
**Key functions**: reset

### `tests/test_smart_read.py` (28 lines)
**Purpose**: test_smart_read module
**Key functions**: load_

### `tests/test_storage.py` (852 lines)
**Purpose**: Test coverage for src/data/storage.py — async SQLite storage backend.
**Classes**: TestRecordConstructors, TestInitializeAndClose, TestOpenStorageContextManager, TestBars, TestTrades, TestRegimeSnapshots, TestModelMetrics, TestEquityCurve, TestValidateSymbol, TestAuditLog, TestHealthCheck, TestSchemaMigrations, TestMissedTrades
**Key functions**: make_

### `tests/test_strategies_filters.py` (518 lines)
**Purpose**: Test coverage for src/strategies/filters.py — research-backed signal filters.
**Classes**: TestEwmTrendSignal, TestTrendFilterPasses, TestVolAdjustedMomentum, TestOvernightGapIsExcessive, TestRegimePositionScaler, TestHurstExponent, TestHurstFilterPasses, TestObvTrendConfirms, TestVolExplosionBlocks, TestMtfTrendAligned, TestApplyAllStrategyFilters

### `tests/test_timescale_storage.py` (899 lines)
**Purpose**: GAP-006: tests for TimescaleBackend — run against the live local TimescaleDB
con
**Classes**: TestHelpers, TestInitializeAndClose, TestSchemaMigrations, TestBars, TestTrades, TestRegimeSnapshots, TestMissedTrades, TestModelMetrics, TestEquityCurve, TestValidateSymbol, TestAuditLog, TestHealthCheck, TestIntelligenceFeatures
**Key functions**: test_

### `tests/test_trade_auditor.py` (267 lines)
**Purpose**: Coverage for:
  - src/diagnostics/trade_auditor.py
  - src/risk/intelligence_gat
**Classes**: TestAuditRecord, TestTradeAuditor, TestDetectAnomalies

### `tests/test_tuning_audit.py` (49 lines)
**Purpose**: test_tuning_audit module
**Key functions**: test_

### `tests/test_tuning_backtest_harness.py` (430 lines)
**Purpose**: test_tuning_backtest_harness module
**Key functions**: test_

### `tests/test_tuning_bootstrap.py` (213 lines)
**Purpose**: test_tuning_bootstrap module
**Key functions**: test_

### `tests/test_tuning_evaluator.py` (176 lines)
**Purpose**: test_tuning_evaluator module
**Key functions**: test_

### `tests/test_tuning_gate.py` (88 lines)
**Purpose**: test_tuning_gate module
**Key functions**: make_

### `tests/test_tuning_live_overrides.py` (145 lines)
**Purpose**: Tests for src/tuning/live_overrides.py -- the seam that surfaces promoted
self-t
**Classes**: TestHMMOverlay, TestRiskOverlay, TestFeatureOverlay, TestXGBoostOverlay

### `tests/test_tuning_proposer.py` (63 lines)
**Purpose**: test_tuning_proposer module
**Key functions**: make_

### `tests/test_tuning_registry.py` (132 lines)
**Purpose**: test_tuning_registry module
**Key functions**: make_

### `tests/test_tuning_runner.py` (194 lines)
**Purpose**: test_tuning_runner module
**Key functions**: make_

### `tests/test_tuning_scheduler.py` (638 lines)
**Purpose**: Tests for src/tuning/scheduler.py -- AutoTuningScheduler.
**Classes**: _FakeStorage, TestShannonEntropy, TestAutoTuningSchedulerAttempts, TestBuildSlippageSamples, TestAutoTuningSchedulerSlippageAttempt, TestFeatureWindowAttempt, TestXGBoostHyperparamAttempt
**Key functions**: make_

### `tests/test_tuning_store.py` (86 lines)
**Purpose**: test_tuning_store module
**Key functions**: test_

### `tests/test_tuning_watchdog.py` (107 lines)
**Purpose**: test_tuning_watchdog module
**Key functions**: make_

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
