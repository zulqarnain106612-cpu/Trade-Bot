# Trade-Bot Codebase Knowledge

**Purpose:** standalone architecture reference for engineers and code-generating LLMs changing this repository.  
**Source audit baseline:** branch `codex/adaptive-horizon-intelligence`, commit `9128e3da8b9542efef5c89706b8f2e8f3512f054` (2026-10-12).

**Indexed source baseline:** `main` source commit `6232016c94db2025e9afddf4aa3e3e2331dc7b2f`; this knowledge pack was merged into `main` by `7dc29702a728bc18365bfc41afae1a8017d8878b` on 2026-10-13. The documentation merge does not mean the source audit was rerun or that CI passed. FILE_INDEX metadata was refreshed against the PR worktree based on that source baseline. The CHUNK_INDEX section for `src/workers/orchestrator.py` was reconciled to main, including the five-line insertion; other semantic anchors remain manually maintained and are checked for hash/range drift by `scripts/codebase_knowledge_index.py`. The two source files changed between the audited baseline and main are documented in PHASE_2_SOURCE_AUDIT.md. This is source inspection, not a CI, live-exchange, deployment, or production-readiness certification.

## How to use this document

Start with architecture and safety rules before changing code. Locate the owning subsystem, then trace entry point → caller → state owner → external boundary → output/error handling → deciding tests. Supplementary references:

- **codebase-analysis-docs/FILE_INDEX.md** — inventory, priority, line counts, SHA-256 prefixes.
- **codebase-analysis-docs/CHUNK_INDEX.md** — function/class range index for critical source files.
- **codebase-analysis-docs/API_REFERENCE.md** — FastAPI route-to-handler list.
- **codebase-analysis-docs/STATE_BLOCKS.md** — per-phase findings, questions, risks and glossary deltas.
- **codebase-analysis-docs/PHASE_2_SOURCE_AUDIT.md** — source-backed subsystem audit, findings, and verification limitations for 2026-10-13.
- **codebase-analysis-docs/assets/architecture.mmd**, **trading-flow.mmd**, **runtime-change.mmd**, **schema-er.mmd**, **terminal-architecture.mmd** — Mermaid source diagrams.

The code is authoritative for current behavior. If source, test, policy and prose disagree, record the contradiction and trace the current caller/wiring and test contract. The inventory is broad; deep reading prioritized startup, trade flow, risk, persistence, API/auth, runtime control, frontend, terminal, testing and developer tooling. This document does not claim every one of the 1,215 tracked application/repository files was read line-by-line; the knowledge-pack files themselves are excluded from FILE_INDEX to avoid self-referential hashes.

## 1. High-level overview

### 1.1 What the application is

Trade-Bot is an asynchronous Python 3.11 algorithmic cryptocurrency-trading platform configured for Binance as primary venue and OKX as secondary. The main runtime gathers market data, persists bars, calculates model/strategy signals, enforces sequential risk checks and uses paper or live execution adapters. FastAPI exposes state and operator actions; a React/Vite dashboard and an Electron Linux desktop shell consume it. SQLite is the default durable store, and TimescaleDB is supported.

The checkout is broader than one trading loop. It also has a separate host-user terminal/process service, runtime desired-state/lifecycle control, optional horizon-oriented crypto intelligence and Bitcoin-chain analytics, eighteen specialist analytic engines, and developer-facing retrieval/knowledge-graph/LLM-tooling systems. These systems are related, not one undifferentiated data path.

### 1.2 Features and business purpose

| Capability | Business need | Primary source |
|---|---|---|
| Market data | Obtain fresh, ordered bars and venue context | **src/data/fetcher.py**, **src/data/quality_gate.py**, **src/data/clock.py** |
| Signal generation | Convert market history into direction and trade/no-trade estimates | **src/features/pipeline.py**, **src/regime/**, **src/models/trainer.py**, **src/engine/signal_engine.py** |
| Intelligence enrichment | Incorporate derivatives, exchange flow, macro and on-chain context when available | **src/intelligence/**, **src/features/intelligence_features.py** |
| Risk and sizing | Bound exposure, refuse unsafe trades and size permitted signals | **src/risk/**, **src/strategies/position_sizing.py** |
| Portfolio | Compare strategy families and allocate capital | **src/strategies/**, **src/engine/orchestrator.py**, **src/tuning/meta_allocator.py** |
| Execution/recovery | Track order submissions, partial fills, cancellations, timeouts and reconciliation | **src/execution/**, **src/diagnostics/disaster_recovery.py** |
| Persistence/audit | Store market history, trades, metrics, equity, decisions and runtime intent | **src/data/storage.py**, **src/data/timescale_storage.py**, **src/diagnostics/** |
| API/dashboard | Give an authenticated operator status, history and safe control surfaces | **src/api/main.py**, **frontend/src/App.jsx**, **frontend/src/hooks/useApi.js** |
| Runtime control | Inventory components, record desired state, apply supported actions and reconcile | **src/runtime/**, runtime endpoints in **src/api/main.py** |
| Terminal/process center | Provide real host-user shell sessions without embedding shell access in the trading API | **src/terminal/**, **frontend/src/terminal/** |
| Horizon / ECC analytics | Compute auxiliary multi-horizon predictions and blockchain/ECC-derived features | **src/intel.py**, **src/workers/orchestrator.py**, **src/ecc/**, **config/horizons.yaml** |
| Analytic engine bank | Run specialist statistical, microstructure and other named analyses | **src/engines/** |
| Repository assistance | Retrieve repository knowledge and coordinate guarded tool execution | **common/**, **orchestrator/**, **tools/**, **kg/**, **rag_mongo/**, **review/**, **src/agent_control/** |

### 1.3 Behavior-affecting defaults

Verified in **src/config.py**:

- Trading mode defaults to PAPER; live trading requires deliberate configuration and eligibility checks.
- Main symbol is BTC/USDT. Active streams are 1m, 15m and 4h, with 15m primary by default.
- Execution mode is MANUAL by default. AUTOMATIC avoids approval requests only within risk limits. RESTRICTED allows automatic execution below the configured notional limit and requires approval above it, with timeout skip. MANUAL queues every trade for operator decision.
- Binance testnet and OKX demo settings default on; credentials default empty.
- SQLite is the default backend; TimescaleDB is opt-in.
- Self-tuning is disabled by default and shadow mode enabled.
- Only the model-driven strategy is enabled by default. Other strategy families exist but default disabled.

These are application defaults, not assertions about a host's actual environment. Never read or publish a developer's ignored local environment file or secrets when validating configuration.

### 1.4 Architecture overview

The authoritative application path is:

    Operator / Dashboard
            │ authenticated HTTP + WebSocket
            ▼
    FastAPI lifespan and routes (src/api/main.py)
            │ startup/shutdown composition
            ▼
    Orchestrator (src/engine/orchestrator.py)
            │ concurrent 1m / 15m / 4h loops
            ├── MarketDataFetcher ── exchange REST / WebSocket
            ├── SignalEngine ── features ── HMM / classifiers ── risk gates
            ├── paper/live executor ── order FSM / order manager
            ├── strategy portfolio / allocation / position monitor
            └── storage + event bus + diagnostics + runtime platform

Full diagram: **codebase-analysis-docs/assets/architecture.mmd**. Tick sequence: **codebase-analysis-docs/assets/trading-flow.mmd**.

**Decisions / findings:** the repository contains the trading service, dashboard/desktop shell, terminal daemon, runtime control plane, optional horizon analytics and separate developer retrieval/tooling systems.  
**Open questions:** the deployed environment and actual override settings are unknown.  
**Next steps:** treat this map as orientation, then confirm the current caller and settings source before modifying behavior.

## 2. System architecture deep dive

### 2.1 Startup, process ownership and shutdown

**src/api/__main__.py** loads settings, logging and starts Uvicorn. The FastAPI application object and lifespan are in **src/api/main.py**.

The lifespan is the main composition root:

1. Configure logging and fail startup on missing API/operator secrets.
2. Validate CORS and warn on non-loopback binding without configured TLS.
3. Register configuration-enabled strategies.
4. Create and initialize the selected store.
5. Open the market-data fetcher and start venue initialization as a tracked task.
6. Construct the main **Orchestrator** and run its startup/warm-up preparation.
7. Mark security controls healthy only after startup built them; restore saved execution mode/halt state.
8. When INTEL_ENABLED=true, attempt to start the optional CryptoIntelligence path and attach its adapter. Failure is logged separately.
9. Subscribe to bus topics before starting event consumers; start the runtime platform's initial reconcile, trading loop, heartbeat and WebSocket fan-out, and runtime trace/reconcile work.
10. Start AutoTuningScheduler only when its master setting is enabled. Mark base startup state ready after the platform's initial reconcile and tasks are running; model/readiness evidence remains separately exposed.

Shutdown closes subscriptions, cancels consumers, flushes runtime/audit state where possible, stops tuning/intelligence, stops and joins the orchestrator, shuts down the fetcher and closes job reporting/storage. **src/api/main.py::_StartupGuard** exists to release partially created resources after startup failure. Preserve lifecycle ownership and cleanup ordering when adding tasks.

**src/workers/trader.py** is a headless alternative that starts the same core Orchestrator without the HTTP surface. It intentionally omits API-only IntelligenceAdapter autostart and AutoTuningScheduler autostart. **docker-compose.yml** makes the worker opt-in and warns not to run it alongside API: API lifespan already starts its own trading loop. Running both on one account/store can create two competing loops. Do not remove this separation without a verified single-owner mechanism.

### 2.2 Trading tick and data flow

A per-timeframe loop in **src/engine/orchestrator.py** ticks a **SignalEngine** on UTC-aligned bar boundaries. The conceptual flow is:

1. Fetch/backfill bars and store them.
2. Load recent history for the selected symbol/timeframe.
3. Check continuity/freshness and construct an ordered feature matrix.
4. Estimate the market regime with the HMM and any configured changepoint/ensemble signals.
5. Calculate direction and meta-label probabilities.
6. Apply active supplemental ensemble or online-learning inputs, if their configuration/path allows.
7. Calculate candidate position size and apply configured sizing scalars/caps plus sequential risk gates.
8. Return **SignalResult** with tradeability, direction, probabilities, regime, sizing, gate outcome and no-trade reason.
9. The orchestrator routes eligible intents through execution/approval rules and records outcomes.
10. Publish decision/order/metric events for storage, audit, monitoring and dashboard display.

The actual gate sequence is load-bearing: **src/risk/gates.py::evaluate_all_gates** returns on the first failure. Do not reorder gates, convert vetoes to penalties, or collapse gate/readiness conditions without tracing consumers and updating contracts/tests.

### 2.3 Components and responsibilities

| Layer | Source | Responsibility |
|---|---|---|
| Config | **src/config.py**, **config/*.yaml**, **.env.example** | Validated environment/settings; core trading config versus separate intelligence/horizon config |
| I/O | **src/data/fetcher.py**, **src/data/orderbook_stream.py**, **src/data/clock.py** | CCXT venues, OHLCV/books, retry/backoff, timing and skew |
| Durable data | **src/data/storage.py**, **src/data/timescale_storage.py** | Shared async persistence contract and schema migrations |
| Analytics/model | **src/features/**, **src/models/**, **src/regime/**, **src/fusion/** | Ordered feature computation, training/inference, model versioning, regimes and auxiliary horizon fusion |
| Decisions | **src/risk/**, **src/strategies/filters.py**, **src/strategies/position_sizing.py** | Hard vetoes, sizing, exits and strategy limits |
| Execution | **src/execution/** | Paper/live adapters, order state, throttling, idempotency and ledger |
| Orchestration | **src/engine/orchestrator.py**, **src/engine/signal_engine.py**, **src/workers/trader.py** | Lifecycle, timeframe loops, training/backfills, portfolio and order coordination |
| Observability | **src/eventbus/**, **src/diagnostics/**, **src/logging_setup.py** | Structured eventing, audit chain, trace, metrics, drift and recovery |
| API/security | **src/api/** | HTTP/WebSocket, authorization, error sanitation, object-ID validation and headers |
| Runtime control | **src/runtime/** | Component capabilities, versions, desired-state changes, approvals, supervision and reconciliation |
| UI/desktop | **frontend/src/**, **frontend/electron/** | Dashboard, API hooks, persistent workspace, terminal transport |
| Local host terminal | **src/terminal/**, **deploy/systemd/** | Separate Linux user daemon, PTYs, sessions and bounded process history |
| Developer tools | **common/**, root **orchestrator/**, **tools/**, **kg/**, **rag_mongo/**, **review/**, **src/agent_control/** | Guarded command execution, retrieval, graph construction and agent task/worktree state |

### 2.4 Events, logs and metrics

**src/eventbus/** implements asynchronous topic pub/sub; **src/eventbus/envelope.py** carries correlation identifiers. Order FSM events are emitted after state commit. API fan-out maps event topics to permitted WebSocket subscribers. Runtime lifecycle events and decision traces use the same broad eventing/diagnostic approach.

Subscribe before starting a producer when dropping its first events is unacceptable. Close subscriptions before cancelling consumers so queue waiters are not stranded on a shutting-down event loop. Observers should be bounded/drop-tolerant rather than allowing a slow dashboard or audit reader to block order submission.

Structured logs use structlog configured by **src/logging_setup.py**. Prometheus metrics are composed in **src/diagnostics/metrics.py** and served by the API. Diagnostics also include decision traces, hash-chained audit, strategy attribution, feature/model drift, fail-safe policies, halt triggers, recovery objectives and restore drills.

### 2.5 External integrations

| Integration | Use | Boundary |
|---|---|---|
| Binance, OKX via CCXT | Market data and live order execution | API credentials and execution mode remain separate from public feed availability |
| SQLite via aiosqlite | Default durable history | WAL/migrations in **src/data/storage.py** |
| PostgreSQL/Timescale via asyncpg | Alternative durable history | Selected explicitly; migrations in **src/data/timescale_storage.py** |
| DuckDB | In-process intelligence/OLAP | **src/data/duckdb_store.py**, not the main financial trade ledger |
| CoinGecko, Dune, Arkham, Coinglass, DeFiLlama, Glassnode, CryptoQuant and exchange providers | Optional external indicators | Provider code/caching is under **src/intelligence/**; secrets/config in settings; verify each provider's fallback semantics before changing it |
| MLflow / optional model tooling | Auxiliary model registry/experimentation | Dependencies split between runtime and optional requirement files |
| MongoDB Atlas / Vector Search | RAG and knowledge graph for repository assistance | **rag_mongo/** and **kg/**; not trading persistence |
| GitHub Actions | Supported test/build/lint/security/gate execution | Defined in **.github/workflows/**; local project policy blocks these commands and blocks bulk CI log/check retrieval |

**Decisions / findings:** FastAPI lifespan owns the API-mode main trading loop; the headless worker is a mutually exclusive alternative.  
**Open questions:** deployed TLS, proxy, provider credentials and database selection were not inspected.  
**Next steps:** follow startup and shutdown callers before introducing a long-lived service or background task.

## 3. Feature-by-feature analysis

### 3.1 Market data, freshness and time

**Business purpose:** make every downstream feature/model consume the intended bar and venue context, not a stale, partial or misaligned sample.

- Entry: the main orchestrator, plus API-triggered backfill.
- Core: **src/data/fetcher.py::MarketDataFetcher**, **src/data/quality_gate.py::DataQualityGate**, **src/data/clock.py**, **src/data/orderbook_stream.py**, storage bar methods.
- Side effects: asynchronous venue clients, market metadata caching, retry/backoff, persisted OHLCV, optional book stream and venue events.
- Contracts: timestamps are UTC Unix milliseconds; bars are unique by symbol/timeframe/timestamp; training expects chronological data. Use sanctioned clock helpers, not naive local wall time.
- Edge cases: retry exhaustion, partial venue outage, malformed/null numeric fields, gap/missing bars, clock skew, stale cache, cancellation during startup/shutdown. Treat unavailable data as unavailable; do not silently relabel it fresh.
- Change test map: inspect **tests/** data/provider/time/signal tests and the corresponding entry in **config/quality_registry.json**.

### 3.2 Features and labels

**src/features/pipeline.py** implements fractional differentiation, rolling VWAP deviation, OHLCV-derived OFI fallback, realized-volatility ratio, ATR-normalized momentum, rolling Sharpe, volume z-score, triple-barrier labels and meta labels. Main feature entry points include **build_feature_matrix** and **build_inference_features**. Intelligence feature joins live in **src/features/intelligence_features.py** and **_inject_intelligence_features**.

The contract includes column identity and order, transform parameters, label horizon, timestamps and null treatment. Before introducing a feature verify that training and inference construct the same ordered columns; its inputs exist at decision time; it has acceptable coverage; it cannot leak future outcomes; and optional-provider failure degrades safely. Align label lookahead and configured purge/embargo with any horizon changes.

### 3.3 Regimes and classifiers

**src/regime/detector.py** contains the Gaussian HMM path. The project's conventional labels are ranging=0, trending=1, volatile=2; the configured volatile index and downstream mapping must remain aligned. **src/regime/changepoint.py** and **src/regime/ensemble.py** supplement regime assessment and capture disagreement.

**src/models/trainer.py::ModelTrainer** trains both direction and meta-label classifiers with combinatorial purged cross-validation, purge and embargo. It computes out-of-sample metrics and atomically writes model files with SHA-256 manifests. **src/engine/signal_engine.py::SignalEngine.tick** loads bars/features, estimates regime, predicts direction/meta probabilities and returns the structured SignalResult used by the orchestrator.

**src/models/model_registry.py** plus shadow methods in SignalEngine keep the candidate/incumbent boundary. The direction model and meta model trained against it are one bundle: promotion must not mix a new direction model with an incompatible meta model. Keep metrics publication consistent with promotion; a rejected candidate must not create a false live-gate pass.

**Online-learning documentation discrepancy:** current SignalEngine source imports **src/models/online_trainer.py** and contains **_record_p_long_for_bar**, **_learn_online** and **_blend_online**. A README paragraph says online adaptation is not wired. That narrative conflicts with the observed structure. Follow the runtime branch conditions, label-resolution path and actual calls at current HEAD before asserting the feature is active or inactive.

### 3.4 Intelligence and horizon prediction

There are two distinct but related paths:

1. Core feature context is aggregated in **src/intelligence/** and **src/features/intelligence_features.py**. Provider results can include funding/open interest/basis, exchange flow and on-chain signals; caches and provider adapters isolate latency/failures. This enriches the core feature set and can reduce confidence when unavailable.
2. The horizon subsystem uses **src/intel.py**, **src/workers/orchestrator.py**, **src/models/horizon_*.py**, **src/fusion/**, **src/ecc/** and **config/horizons.yaml**. Configured horizons h1–h10 range from 30 seconds to one month. They are not the same as the core execution streams of 1m, 15m and 4h. API lifespan starts CryptoIntelligence only when INTEL_ENABLED=true; headless worker does not autostart that adapter.

ECC modules process cryptographic/blockchain patterns such as ECDSA nonce-reuse signals, Schnorr/Taproot flows, UTXO clusters and age curves. Treat output as analytical feature context, not execution authority or proof that a private key was recovered. **config/risk.yaml**, **config/intelligence.yaml** and **config/horizons.yaml** belong to this auxiliary path and must not be confused with core **src/config.py**.

### 3.5 Risk and position sizing

**Purpose:** prevent entry or reduce size when signal, data, transaction economics, model history or portfolio conditions violate policy.

**src/risk/gates.py** defines ordered checks such as slippage-cost veto, capital-preservation drawdown floor, daily drawdown, consecutive-loss halt, regime volatility, max position size, live-model eligibility and minimum paper-trading period. Additional checks cover performance drift, exchange stress and whale activity. **evaluate_all_gates** short-circuits at the first failure, which determines both behavior and displayed/audited skip reason. Consult exact source before documenting or changing the complete execution sequence.

Sizing uses **src/risk/kelly.py** and **src/strategies/position_sizing.py**, with optional reductions/caps from regime agreement, strategy/portfolio correlation, macro exposure, CVaR and other helpers. Correlation may shrink size rather than veto the signal. Exit monitoring is distinct from entry gates.

Units are especially important:
- In core **src/config.py**, daily drawdown halt 2.0 is percentage units (2%), and maximum single-position size 5.0 is percent units (5%).
- **capital_preservation_max_drawdown_pct=0.30** is a fraction (30%) despite the suffix. Its halt persists and needs explicit re-authorization; it is not daily drawdown.
- Auxiliary **config/risk.yaml** uses separate fields/defaults such as a 0.10 drawdown floor and 0.02 daily loss threshold. Same names do not imply same consumer or unit.
- **whale_gate_advisory** defaults false in core settings. Any claim that whale activity only reduces size must check the actual config and gate branch.
- Model gate includes minimum OOS Sharpe, maximum drawdown and minimum trade count; paper-duration qualification is another control. Do not merge those criteria into one generic “model healthy” boolean.

### 3.6 Strategy registry and portfolio

**src/strategies/registry.py** defines an intentionally narrow **StrategyProtocol**: stable strategy_id, **generate_signal(bar)** and **required_capital_fraction()**. The shared signal has direction in {-1,0,1}, confidence in [0,1], and regime fit in [0,1]. The registry checks unique IDs and the capital ceiling.

**src/strategies/bootstrap.py::register_default_strategies** registers configuration-enabled strategy families: model signal engine, pairs mean reversion, breakout, funding carry, cross-sectional momentum, basis, cross-exchange arbitrage and options carry. Default settings enable only the model signal engine. Registering a strategy does not itself submit orders; the orchestrator must build context and evaluate it.

**src/engine/orchestrator.py** assembles portfolio inputs and evaluates registered families. Capital fractions are ceilings, not fixed allocations that must sum to one; allocation is performance-weighted and rate-limited. A ceiling total below one means the book cannot be fully deployed. Attribution and kill switches depend on stable IDs. Re-enable behavior is promotion-gauntlet-gated.

### 3.7 Execution and recovery

Key files: **src/execution/base.py**, **paper.py**, **live.py**, **order_fsm.py**, **order_manager.py**, **idempotency.py**, **order_throttler.py**, **unified_ledger.py**.

Paper/live executors implement a common contract. OrderFSM permits guarded state transitions: PENDING can go to FILLING, TIMEOUT or FAILED; FILLING can remain FILLING or transition to FILLED, CANCELLED, TIMEOUT or FAILED. FILLED/CANCELLED/TIMEOUT/FAILED are terminal and immutable. Partial fills are accumulated and average fill price is volume weighted. A small relative overfill tolerance prevents floating-point drift from rejecting the final fill. Events are emitted after state is committed.

Keep the idempotency key and exchange order identity stable across retries/recovery. A network timeout does not prove that the venue rejected an order. The unified ledger nets signed same-symbol positions within a venue across timeframes rather than allowing later legs to overwrite earlier ones. Durable trade history, runtime ledger and equity snapshots are different state owners; keep their semantics distinct. Recovery endpoints surface reconciliation and a separate acknowledgement path.

### 3.8 Self-tuning and upgrades

**src/tuning/** provides a bounded tunable registry, version store, proposer, evaluator/backtest harness, promotion gate, runner, audit log, pause state and post-promotion watchdog. The scheduler only starts when enabled. Shadow mode is enabled by default. Any change must pass propose/evaluate/gate/observation/probation/rollback policy; do not create a second path that writes live config directly.

**src/upgrade/** supports MAML, Optuna walk-forward studies, an MLflow registry facade and shadow deployment. Candidate models and parameters must continue to use the existing promotion controls and record their provenance/results.

### 3.9 Runtime lifecycle and desired state

**src/runtime/** tracks long-lived components with typed identities, immutable version identity, declared capabilities, lifecycle states, health, desired state and transition history.

- **contracts.py:** identity/version/state/action/capability/desired-state contracts and transition table.
- **registry.py::RuntimeRegistry:** record/component registry, transitions, version history and observed/desired state.
- **adapters.py:** discovery into the existing subsystem registries, not a replacement registry for each domain.
- **controllers.py:** the explicit production control operations.
- **supervisor.py:** per-component locking, declared controller execution and failure/quarantine policy.
- **dependencies.py** and **classification.py:** dependency cycles/impact and change class.
- **changes.py::ChangeManager:** classification, policy/approval, stage, execution/observation, promotion or rollback with audit.
- **reconcile.py** and **production.py::RuntimeLoop:** desired-versus-actual diff, discovery and repeated reconciliation.
- **persistence.py**, **events.py** and **platform.py:** durable desired state/audit, event emissions and composition root.

Lifecycle states are DISCOVERED, VALIDATED, INITIALIZED, STANDBY, ACTIVE, PAUSED, DRAINED, STOPPED, FAILED and QUARANTINED. A legal state transition is insufficient unless the component declares the capability. Several adapters are observe-only; runtime is not a general system for rewriting arbitrary Python live. See **docs/runtime/RUNTIME_PLATFORM.md**. Diagram: **codebase-analysis-docs/assets/runtime-change.mmd**.

### 3.10 Dashboard, API and Electron

**frontend/src/main.jsx** is the React entry and **frontend/src/App.jsx** assembles panels. **frontend/src/hooks/useApi.js** centralizes HTTP, polling, WebSocket and operator-action helpers. Panels under **frontend/src/components/panels/** surface trades, approvals, risk controls, models, runtime, venues, intelligence, operations and monitoring. Workspace logic is under **frontend/src/components/workspace/**. Terminal UX uses **frontend/src/components/terminal/** and **frontend/src/terminal/**.

HTTP calls send the API key through the x-api-key header. The browser WebSocket uses api_key in the query string as a fallback because browser WebSocket cannot set arbitrary upgrade headers; query values are more likely to appear in access logs. Electron uses context isolation, disabled renderer Node integration and a narrow IPC bridge. Keep both authority and origin validation separate.

Full route-to-handler index: **codebase-analysis-docs/API_REFERENCE.md**.

### 3.11 Terminal/process center

The terminal is a separate per-user daemon, not a PTY hosted inside the API. Entry points include **src/terminal/__main__.py**, **src/terminal/cli.py**, **src/terminal/server.py**; desktop bridging is **frontend/electron/terminalBridge.cjs**. Host CLI and dashboard share sessions, so closing a window need not kill shell processes.

It uses actual Linux PTYs, session/process-group management, bounded output buffers and process history. Host CLI IPC is a Unix socket with restrictive permissions and peer UID verification. Browser development transport is a loopback WebSocket using a separate first-frame token, Origin allow-list, Host/loopback check and hello deadline. Electron relays only validated message types. The API can report long-running jobs through the socket as best-effort events; that must not block order submission.

Known constraints: terminal design depends on Linux PTY, /proc and SO_PEERCRED behavior; Windows/macOS are not claimed. Exact exit codes depend on shell integration (Bash), and unsupported shells should report unknown rather than invented status. Background command discovery is not a complete process inventory, and reattached full-screen tools may require redraw. See **docs/terminal/ARCHITECTURE.md**, **docs/terminal/OPERATIONS.md** and **codebase-analysis-docs/assets/terminal-architecture.mmd**.

### 3.12 E-01 to E-18 specialist engines

**src/engines/** contains eighteen specialist engines, consensus, schema, risk quantifier and signal gate. This is distinct from the primary **src/engine/signal_engine.py**. Project policy says **src/engines/orchestrator.py** associates engines to E-01…E-18 positionally. Reordering the list changes attribution, SLA and logs even if every engine remains callable. Preserve the producer/consumer seam and **tests/test_engine_seam_contract.py**. Empty input must yield a valid abstention; a cycle should report every engine output or a named failure.

### 3.13 Developer tooling / RAG / knowledge graph

These tools support repository work and do not belong automatically to the trade tick path.

- **common/command_schema.py** classifies command effects and blocks unsafe/CI-data access; **common/shell_exec.py** enforces command declarations, retries and bounded/redacted output.
- Root **tools/registry.py** registers tools. Root **orchestrator/** and **orchestrator_cli.py** implement plan/execute/synthesize for eligible tools; read-only safe operations do not imply that irreversible actions are safe to parallelize.
- **rag_mongo/** chunks/embeds content and retrieves with MongoDB Atlas vector/full-text search.
- **kg/** extracts triples, resolves entities, assembles and queries graph structures.
- **review/** provides vector/full-text/hybrid/graph retrieval for grounded review context.
- **src/agent_control/** tracks task manifests, completion/evidence, Git branches/worktrees, workspace validation and reliability. It is separate from **src/runtime/** lifecycle state.
- Ingestion/CLI entry points include **rag_cli.py**, **kg_cli.py**, **orchestrator_cli.py**, **scripts/ingest_repo.py** and **scripts/ingest_kg_repo.py**.

Do not confuse the following names: **src/engine/orchestrator.py** coordinates actual trades; **src/engines/orchestrator.py** combines E-01…E-18; **src/workers/orchestrator.py** runs horizon/ECC worker tasks; **src/runtime/production.py::RuntimeLoop** reconciles desired state; root **orchestrator/** controls developer tool execution.

**Decisions / findings:** the main trade decision path is market data → ordered features/models → risk → execution → ledger/audit; side systems are opt-in or separately owned.  
**Open questions:** the online-learning activation path and provider-specific fallbacks need a caller-level trace for changes involving them.  
**Next steps:** locate the owning feature, its shared contract, its registry invariant and deciding tests before editing.

## 4. Deep reference

### 4.1 Configuration

The primary runtime settings authority is **src/config.py**, a Pydantic Settings tree with exchange, risk, HMM, XGBoost, feature, storage, API, intelligence, self-tuning, strategy portfolio, throttle and strategy sections. **get_settings()** is cached. **RuntimeConfig** manages mutable execution/risk controls and **_LiveSettings** applies bounded overrides via revalidation.

Separate YAML files:
- **config/horizons.yaml**: h1–h10 time horizons (30s, 2m, 5m, 15m, 1h, 4h, 1D, 3D, 1W, 1M) and model declarations.
- **config/intelligence.yaml**: optional worker count, inference timeout, freshness, ECC, checkpoint, DuckDB and observability settings.
- **config/risk.yaml**: separate horizon/intelligence risk and circuit-breaker settings.
They do not replace core **src/config.py**. Before changing a parameter, locate all readers/writers/tests; similarly named YAML and Pydantic fields need not share units or semantics.

### 4.2 Durable schema and logical relations

Schema and migrations originate in **src/data/storage.py**. **src/data/timescale_storage.py** provides the corresponding backend. SQLite migrations are ordered and tracked with PRAGMA user_version; Postgres uses a schema_version table. SQLite DDL comments warn against declaring a column both in base schema and a later migration (it creates duplicate-column errors on fresh install).

| Table | Identity key | Primary purpose |
|---|---|---|
| bars | unique symbol, timeframe, ts | OHLCV history; timestamp is UTC Unix milliseconds |
| trades | string id (UUID-like) | Entry/exit, side/direction, execution mode, quantity/notional, P&L, fee and model/risk context |
| regime_snapshots | unique symbol, timeframe, ts | Per-bar regime probabilities and later agreement/changepoint data |
| model_metrics | unique model_name, timeframe, version | OOS Sharpe, drawdown, trade count and classification metrics for live gate |
| equity_curve | unique ts and trading_mode | Equity, cash, P&L and drawdown; paper/live snapshots may share a timestamp |
| audit_log | integer id | Operator/action event, timestamp and details |
| missed_trades | string id | Signals not opened, reason and decision context |
| intelligence_features_history | symbol, timeframe, bar_ts | Provider feature values, provenance/confidence and later horizon decision fields |
| runtime_desired_state | component_id | Persisted desired state for runtime reconciliation |

**codebase-analysis-docs/assets/schema-er.mmd** shows logical links through symbols/timeframe/timestamps. Its arrows are conceptual correlations, not claims that SQL foreign keys are declared. The unified ledger is in-memory book state and is not the same thing as durable trade history or equity snapshots.

### 4.3 Auth, authorization and API hardening

**src/api/auth.py** uses hmac.compare_digest. API_SECRET_KEY must be configured and strong. Optional API_READONLY_KEY must be long enough and different from the primary key; invalid server-side configuration is a 503, invalid/missing client key a 401. The primary key returns trade-authorizing role and the optional read-only key returns read-only role. **src/api/access_control.py** grants permissions by role and fails closed for an unknown role. **requires(...)** in **src/api/main.py** composes route authorization.

OPERATOR_SECRET is a separate second factor for sensitive actions; API startup refuses to proceed when required secrets are missing. Route-level readiness, permission, rate and second-factor guards are not uniform; inspect each route before changing it. An API key must not be treated as proof of operator confirmation.

Related hardening: **src/api/error_hygiene.py** sanitizes structured/validation errors, **src/api/object_refs.py** checks caller-supplied object identifiers and avoids lookup oracles, **src/api/security_headers.py** applies response headers, **src/api/middleware.py** validates CORS and **src/api/ws_guard.py** checks WebSocket frames. Do not expose raw credential values, private venue payloads or full exception strings in responses/logs.

### 4.4 Interface contracts

| Interface | Caller/contract | When changing it |
|---|---|---|
| FastAPI REST/WS | **src/api/main.py**, frontend API hooks | Keep validation, role, readiness, rate limit, operator factor, event topics and response shape correct |
| Exchange/provider I/O | MarketDataFetcher and provider adapters | Preserve symbol/timeframe typing, freshness, cancellation, backoff and redacted failures |
| Storage | AnyStorageBackend, create_storage_backend | Update SQLite and Timescale, migrations, records, queries and tests together |
| Signal | SignalResult, SignalEngine.tick | Preserve abstention, skip reason, trace, model bundle and feature ordering |
| Strategy | StrategyProtocol, Signal, registry | Stable IDs, bounded values, capital ceilings and safe no-signal behavior |
| Order | Abstract executor, OrderFSM, OrderManager | Legal transitions, idempotency, partial fill accounting and reconciliation |
| Runtime component | ComponentSpec, CapabilitySet, DesiredState | Declared capability, supervisor/controller, state graph, approval/stage/audit/rollback |
| Terminal protocol | src/terminal/protocol.py and server dispatch | Authentication, frame/type/size bounds, safe session/process operations and reconnect semantics |
| Frontend | frontend/src/hooks/useApi.js | API origin validation, key handling, reconnect behavior and HTTP/WS role contracts |
| Developer tool | ToolSpec and common command declaration | Effect classification, permission, redaction, output bounds and CI data boundary |

### 4.5 Glossary

| Term | Meaning |
|---|---|
| Bar | OHLCV observation identified by symbol, timeframe and UTC millisecond timestamp |
| SignalResult | Output of one SignalEngine tick: tradeability, direction, probabilities, regime, gate result and possible sizing |
| Direction model | Classifier estimating probability of long direction |
| Meta-label | Classifier estimating whether to take the main model's prediction |
| Regime | Discrete HMM state and posterior probabilities |
| Triple barrier | Label determined from profit, stop and maximum-holding barriers |
| CPCV | Combinatorial Purged Cross-Validation with purge/embargo against leakage |
| OOS | Out of sample; data excluded from a training fold |
| Fractional Kelly | Edge-based size scaled by a conservative multiplier and hard ceilings |
| Live gate | Track-record/model thresholds controlling whether real trading is permitted |
| Capital-preservation floor | Peak drawdown halt needing explicit authorization to re-enable |
| Execution mode | Approval policy (automatic, restricted, manual), independent of paper/live mode |
| Missed trade | Signal that did not result in an open position, with a recorded reason |
| Unified ledger | Cross-venue in-memory book representation distinct from database history |
| Shadow model | Candidate scored beside incumbent without immediate promotion |
| Shadow tuning | Parameter observation without live application |
| Promotion gauntlet | Criteria required before strategy re-enable/promotion |
| Desired state | Persisted target state reconciled against actual runtime state |
| CapabilitySet | Declared runtime actions a component/controller can perform |
| Horizon model | Auxiliary prediction for h1–h10, not core 1m/15m/4h stream |
| ECC analytics | Blockchain/cryptographic structure analysis used as derived context |
| PTY | OS pseudo-terminal with real terminal/process-group semantics |
| RAG | Retrieval-augmented generation using ingested data and retrieval |
| Quality registry | config/quality_registry.json; requirement, invariant, regression and test authority |
| Math registry | config/math_registry.json; authority for mathematical objects relied upon/rejected |
| Architecture layers | config/architecture_layers.json; dependency direction between packages |

### 4.6 Stack and packaging

- Python target is >=3.11,<3.12 in **pyproject.toml**. Core package discovery includes src, kg, rag_mongo, root orchestrator, common, tools and review. **crypto-predictor/** is a separate experiment/backtest package excluded from core coverage source.
- Runtime libraries live in **requirements.txt**; dev/test tools in **requirements-dev.txt**; optional/large extras in **requirements-optional.txt** and **requirements-topology-optional.txt**. Some optional imports are deliberate; trace CI install and import guards before promoting an extra to a hard runtime dependency.
- Frontend uses Node >=22.12, React, Vite, Electron and Vitest; panel/layout/terminal tests are under frontend source. Electron's narrow bridge is not the same as the trading API.
- **Dockerfile** launches python -m src.api as non-root user tradebot (uid 10001). Compose uses TimescaleDB service and binds host-facing ports to loopback. Any production exposure beyond loopback requires a deliberate TLS/authenticated deployment design.

**Decisions / findings:** API contracts, storage keys, migration ownership, configuration scopes and domain terminology are indexed above.  
**Open questions:** endpoint permissions and cross-backend physical schema parity must be established from the affected route/DDL before any behavior change.  
**Next steps:** use API_REFERENCE.md and CHUNK_INDEX.md to open exact source ranges, then inspect adjacent callers/tests.

## 5. Things you must know before changing code

### 5.1 Similar names hide distinct subsystems

Always give the full module path. See section 3.13 for the separate main trade Orchestrator, analytics EngineOrchestrator, horizon WorkerOrchestrator, runtime RuntimeLoop and root developer Tool Orchestrator. Also distinguish **src/engine/** (trade orchestration) from **src/engines/** (18 analytic engines), **src/models/** (core production model code) from root **models/** (artifact/data folder), and **crypto-predictor/** (separate backtesting project).

### 5.2 One authoritative trading loop

The default API and standalone worker each start the same core Orchestrator. Do not run them together against one account/store without an explicit ownership/lease design. Duplicate ticks can submit duplicate intents or race state; this is a financial correctness issue.

### 5.3 Risk and execution are hard boundaries

Live eligibility, capital floor, daily drawdown, consecutive-loss, model qualification, paper-duration and cost gates are separate controls. Keep fail-closed behavior when security controls are unavailable. Do not change “unknown/not ready” to healthy, reorder first-failure gates or weaken limits to make a test pass. Gate ordering shapes operator diagnosis and is part of behavior.

### 5.4 Feature, model and time alignment

- Match feature names/order at training and inference.
- Promote direction and its paired meta-label as one model bundle.
- Tie CPCV purge/embargo to target lookahead.
- Keep UTC milliseconds consistent and use **src/data/clock.py**.
- Preserve missing/stale provider semantics and feature-coverage guards.
- Keep SHA manifest logic coupled with atomic model writes.
- Online learner wiring is present: the orchestrator injects one optional trainer per active timeframe, SignalEngine learns from newly resolved labels and blends online probabilities, and shutdown persists learner state. Treat activation/warm-up as deployment-dependent and optional; see the source-backed Phase 2 audit.

### 5.5 Database migration parity

A durable schema change must be implemented in SQLite and Timescale, including ordered migration/version, records and both query implementations. The SQLite base DDL intentionally omits columns introduced by later migrations. Preserve composite identities for bars, intelligence features and equity mode/time. Logical ER arrows are not a substitute for inspecting DDL.

### 5.6 Concurrency and ownership

- API lifespan owns its tasks, event subscriptions and shutdown.
- Avoid blocking model training or slow network calls on the order/event loop; use existing executor/task tracking and bounds.
- Subscribe before starting a producer when first-event loss is unacceptable; close subscriptions before consumer cancellation.
- Keep provider retry/backoff and resource profiles bounded.
- Stable strategy, order-idempotency and runtime component IDs are behavior contracts.
- Best-effort terminal job reporting may never hold up order submission.

### 5.7 Security boundaries

- Never read/print ignored .env files, keys, tokens, credential stores or unfiltered venue responses in LLM context, logs or documentation.
- Trading API and terminal authorities are independent; tokens are not interchangeable.
- API authentication is not the same as role authorization or the operator second factor.
- The browser WebSocket query-key fallback has higher log exposure than header auth.
- The terminal daemon is local-only and independently validates Origin/Host/token or Unix peer UID. Never expose its transport publicly.
- Preserve error redaction, ID validation, CORS, security headers, rate limits, startup fail-closed controls and readiness distinction.
- Do not add arbitrary shell execution to the API. The terminal process is separate and the repository's developer command path is separately constrained.

### 5.8 Quality gates and permitted verification

Engineering authority is **AGENTS.md**, **CLAUDE.md**, **config/command_policy.json**, **config/architecture_layers.json**, **config/quality_registry.json**, **config/math_registry.json** and the relevant **docs/** policies. Current policy explicitly states GitHub Actions is the only execution route for tests, builds, lint, security scans, workflow validation and quality gates. Local pytest/ruff/Vitest/npm build/pre-commit and local gate scripts are blocked. Direct CI logs, check data and live watchers are blocked too.

- Do not claim tests or CI passed unless permitted current evidence proves it.
- The approved local diagnostic is exactly python3 scripts/local_checks.py prepare; it returns the current HEAD's SHA-bound CI notice only. No tests/build/lint/check command was run as part of this documentation task.
- Gate jobs use if: always(), depend on all other workflow jobs and assess every result. Required workflows support merge_group, and scripts/assert_jobs_green.py decides whether every required job is successful.
- **config/quality_registry.json** is the source of truth for requirements, invariants, regression/security regressions and deciding test paths. **docs/quality/REQUIREMENTS_TRACEABILITY.md** is generated; do not hand-edit it.
- **config/math_registry.json** is the mathematical registry. **scripts/check_static_invariants.py** enforces import cycles and layer boundaries. Do not hide a dependency inversion by expanding the accepted baseline without a documented architectural reason.
- Add required permanent registry and regression test entries with behavior changes; never lower coverage floors or weaken a guard to get a green check. A local pass is not a merge-queue pass.

### 5.9 Existing documentation contradictions

1. **Online learning:** the source path is wired as described above; no contradictory README statement was found in the targeted repository search. If an older external/branch-specific README still says it is unwired, treat that wording as stale against this source snapshot.
2. **Terminal architecture page:** **docs/terminal/ARCHITECTURE.md** preserves a historical implementation baseline at `bbe46cb6`, whose opening table describes the pre-terminal state. Its title/section now explicitly label that content as historical rather than current repository state. For current behavior, trace the present source and tests.
3. **Separate risk configuration:** **config/risk.yaml** is for horizon/intelligence and has different semantics/units than core **src/config.py**.
4. **README endpoint coverage:** the route inventory is not exhaustive. The route reference here was extracted from FastAPI decorators.
5. **Runtime endpoint vs capability:** having an API endpoint does not mean a given component implements arbitrary lifecycle actions; some adapters are observe-only.
6. **Health concepts:** /livez, /readyz and /health are distinct. Model warm-up/readiness, runtime reconcile and process liveness are not one boolean.

### 5.10 Assumptions and confidence register

| Claim | Classification | Confidence / resolution |
|---|---|---|
| API lifespan owns the default trading loop | Source fact | High; src/api/main.py and docker-compose.yml |
| Only model strategy is enabled by default | Source fact | High; src/config.py and src/strategies/bootstrap.py |
| All provider errors are universally fail-open | Not assumed | Provider-specific; inspect caller fallback/data gate |
| Every runtime adapter supports lifecycle writes | Not true universally | Production source/docs distinguish observe-only integrations |
| Both DBs are physically identical in every type/index | Intended common contract, not asserted byte-identical | Compare both DDL/migrations on schema change |
| README contains every endpoint | False for inspected snapshot | Use codebase-analysis-docs/API_REFERENCE.md |
| Online learner is wired into each active timeframe | Source fact at snapshot; runtime activation/weight is deployment-dependent | Orchestrator injects trainer; SignalEngine calls learning once per resolved bar and blends direction/meta outputs; initialization failure is optional degradation |
| Host is live-ready / credentials work | Unknown; not tested | Prove in safe paper/testnet operational environment |

**Decisions / findings:** preserve safety controls, model/feature alignment, SQLite/Timescale parity, stable IDs, lifecycle ownership and the repository’s explicit CI-only gate policy.  
**Open questions:** whether the current deployment has passed required gates is not established by this local source analysis.  
**Next steps:** use the project-approved CI evidence route and add permanent regression/registry coverage for every required invariant.

## 6. Testing and safe-change process

### 6.1 Test map

The inspected working tree contains 571 files under **tests/** (565 Python), plus frontend Vitest suites. Areas include API, authentication/authorization, component/contracts, analytic engines, execution, features, intelligence, models, portfolio, production, property/fuzz, qualification, quality, recovery, regime, regression, risk, runtime, security, supply chain, terminal and trading.

**pyproject.toml** configures pytest over tests, strict marker/config validation, asyncio mode auto, branch coverage and 99.00% required coverage. Core coverage sources include src, kg, rag_mongo, root orchestrator, common, tools and review; crypto-predictor is excluded as a standalone project. A separate CI job enforces per-file floors. These are inventory facts, not a claim that current tests pass or exercise every behavior.

### 6.2 Workflow for future LLMs

1. Confirm branch, HEAD and pre-existing dirty/untracked changes. Preserve user work.
2. Locate the production composition root and all consumers of the affected behavior before changing code.
3. Identify the applicable quality/security/math registry item and deciding tests.
4. Work in the owning subsystem and preserve sibling implementations, especially SQLite/Timescale, REST/WebSocket, API/worker ownership and strategy/model boundaries.
5. Update implementation, regression tests, fixtures and registry entries required by policy. Do not hand-edit generated traceability.
6. Use only the permitted GitHub Actions test/build/lint/security/architecture path. Do not read restricted CI logs/check records.
7. Audit the scoped diff for secrets, changed defaults, migration parity, feature alignment, role guards, state transitions, rollback and unrelated edits.
8. Report exact branch/commit, files, commands actually run, observed evidence and unresolved behavior. Never report planned work as completed or unobserved CI as green.

**Decisions / findings:** the test map and change procedure are documented; no tests/build/lint/security gates were run during this task because local checks are prohibited by project policy.  
**Open questions:** current CI status and actual runtime readiness remain unverified.  
**Next steps:** before a code change, identify required GitHub Actions evidence and a safe paper/testnet validation plan.

## 7. Phase state ledger

See **codebase-analysis-docs/STATE_BLOCKS.md** for Phase 1–6 checkpoints: index version, summary of top files, open questions, risks, glossary additions and next actions. **codebase-analysis-docs/CHUNK_INDEX.md** records file hashes, class/function ranges and chunk anchors. Those indexes are derived evidence to make future investigation cheaper, not a replacement for rereading code that has changed.

**Decisions / findings:** phase checkpoints are recorded in STATE_BLOCKS.md and stable source ranges in CHUNK_INDEX.md.  
**Open questions:** line/hash anchors are valid only for the inspected source hashes.  
**Next steps:** refresh indexes after source edits and append a state checkpoint for future analysis phases.

## 7.1 Stable source anchors

Use these anchors with the matching current hash in codebase-analysis-docs/CHUNK_INDEX.md. Regenerate the index after source modifications.

- API lifespan and resource ownership: [[F:src/api/main.py#584-621#a71ca019]]
- WebSocket authentication/fan-out entry: [[F:src/api/main.py#2007-2044#a71ca019]]
- API key validation and role selection: [[F:src/api/auth.py#94-133#c9a226cf]]
- Primary configuration model: [[F:src/config.py#1013-1050#566fc015]]
- Atomic live risk-control update: [[F:src/config.py#1419-1458#566fc015]]
- Main orchestrator startup: [[F:src/engine/orchestrator.py#433-470#10c49738]]
- Main trading tick: [[F:src/engine/orchestrator.py#1195-1232#10c49738]]
- Retraining and model routing: [[F:src/engine/orchestrator.py#1963-2000#10c49738]]
- Signal computation: [[F:src/engine/signal_engine.py#663-700#c4c078db]]
- Training features and labels: [[F:src/features/pipeline.py#771-808#32403b07]]
- Ordered first-failure risk evaluation: [[F:src/risk/gates.py#669-706#36a7f38f]]
- Order state machine: [[F:src/execution/order_fsm.py#149-186#14f635a5]]
- SQLite initialization/migrations: [[F:src/data/storage.py#745-761#b1db7068]]
- TimescaleDB storage backend: [[F:src/data/timescale_storage.py#394-431#1aa58429]]
- Runtime change validation and policy: [[F:src/runtime/changes.py#292-329#9aa272d8]]
- Runtime discovery/reconciliation: [[F:src/runtime/production.py#266-303#55696d76]]
- Browser transport handshake and dispatch: [[F:src/terminal/server.py#218-230#10681519]]
- Headless runtime ownership: [[F:src/workers/trader.py#53-90#687738d8]]
- Positional E-01…E-18 orchestration: [[F:src/engines/orchestrator.py#308-345#10e2649d]]

**Decisions / findings:** these anchors point into inspected source files by relative path, inclusive line range and full-file hash prefix.  
**Open questions:** any source modification can shift ranges or invalidate a hash.  
**Next steps:** read the indexed declaration and neighboring callers before relying on an anchor.

## 8. Core source index

- Startup / service ownership: **src/api/main.py**, **src/api/__main__.py**, **src/workers/trader.py**, **docker-compose.yml**, **Dockerfile**.
- Signal path: **src/engine/orchestrator.py**, **src/engine/signal_engine.py**, **src/features/pipeline.py**, **src/risk/gates.py**.
- Market data and storage: **src/data/fetcher.py**, **src/data/storage.py**, **src/data/timescale_storage.py**, **src/data/clock.py**.
- Models / regime: **src/models/trainer.py**, **src/models/model_registry.py**, **src/models/online_trainer.py**, **src/regime/**.
- Orders / ledger: **src/execution/**, **src/execution/unified_ledger.py**, **src/diagnostics/disaster_recovery.py**.
- Strategy / tuning / upgrade: **src/strategies/bootstrap.py**, **src/strategies/registry.py**, **src/tuning/**, **src/upgrade/**.
- API security: **src/api/auth.py**, **src/api/access_control.py**, **src/api/runtime_control.py**, **src/api/error_hygiene.py**, **src/api/object_refs.py**, **src/api/security_headers.py**.
- Runtime plane: **src/runtime/contracts.py**, **registry.py**, **supervisor.py**, **changes.py**, **reconcile.py**, **production.py**, **platform.py**.
- UI/terminal: **frontend/src/App.jsx**, **frontend/src/hooks/useApi.js**, **frontend/src/components/workspace/**, **frontend/electron/**, **src/terminal/**.
- Auxiliary analytics: **src/intel.py**, **src/workers/orchestrator.py**, **src/engines/**, **src/ecc/**, **config/horizons.yaml**.
- Repo policies/tools: **AGENTS.md**, **CLAUDE.md**, **config/command_policy.json**, **config/architecture_layers.json**, **config/quality_registry.json**, **config/math_registry.json**, **common/**, **orchestrator/**, **kg/**, **rag_mongo/**, **review/**, **src/agent_control/**.
- Complementary human docs: **docs/runtime/RUNTIME_PLATFORM.md**, **docs/terminal/ARCHITECTURE.md**, **docs/terminal/OPERATIONS.md**, **docs/quality/TEST_STRATEGY.md**, **docs/quality/CI_GATE_ARCHITECTURE.md**, **docs/quality/PRODUCTION_READINESS.md**, **docs/security/THREAT_MODEL.md**, **docs/SCHEMA.md**, **docs/KNOWLEDGE_GRAPH.md**.

**Decisions / findings:** all primary source clusters are listed; supporting file inventory and Mermaid diagrams live beside this master document.  
**Open questions:** this guide cannot establish live-trading safety, external service availability, or passing CI without permitted current operational evidence.  
**Next steps:** refresh this snapshot after architecture/source/policy changes and preserve the local-user workspace state.

---
End of knowledge snapshot. Update this document and the appropriate indexes when implementation, source wiring or repository policy changes.
