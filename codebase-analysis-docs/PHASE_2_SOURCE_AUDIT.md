# Phase 2 — Source-Backed Subsystem Audit

**Audit date:** 2026-10-13  
<<<<<<< HEAD
**Repository snapshot:** `codex/adaptive-horizon-intelligence` at `9128e3da8b9542efef5c89706b8f2e8f3512f054` (source checkout unchanged during this audit).  
=======
**Audit baseline:** `codex/adaptive-horizon-intelligence` at `9128e3da8b9542efef5c89706b8f2e8f3512f054`. The current `main` snapshot for this PR is `6232016c94db2025e9afddf4aa3e3e2331dc7b2f`; the audit has not been fully re-run against that later source state, so verify current callers/tests before treating baseline findings as current.  
>>>>>>> origin/main
**Purpose:** verify current behavior against source, tests, registries, and documentation; separate source facts from unverified runtime claims.  
**Validation boundary:** no tests, builds, lint/type/security checks, quality gates, CI run records, live exchange calls, or credentials were inspected. The repository policy reserves those checks for GitHub Actions and permits only the exact compact SHA-bound notice command locally when preconditions are met.

## Findings by subsystem

### 1. Startup, shutdown, and readiness

**Verified source path:** `src/api/main.py::lifespan`, `src/engine/orchestrator.py::startup/run/shutdown`, `src/api/readiness.py::readiness_report`.  
**Deciding tests:** `tests/test_startup_readiness.py`; `RES-025`, `REG-0026`, `REG-0029` in `config/quality_registry.json`.

- The API lifespan checks required API/operator secrets, validates CORS, initializes storage/fetcher/orchestrator, marks security controls healthy, restores persisted execution mode, subscribes event consumers before producer loops, starts runtime reconciliation and background tasks, then marks the API startup-ready.
- Model warm-up is deferred to the orchestrator run task. `/livez` answers process liveness; `/readyz` separately evaluates API state, runtime platform, model readiness, and timeframe-loop state. A first tick is reported separately and is not required for readiness.
- On shutdown the lifespan marks stopping, stops runtime reconciliation, closes event subscriptions before cancellation, drains tasks, flushes runtime state, stops the orchestrator, shuts down training pools and closes storage. Failed-startup cleanup is separately implemented.
- **Maintenance note (not classified as a correctness defect):** the post-`yield` cleanup closes/cancels/gathers the WebSocket tasks once in the `finally` block and repeats those operations immediately afterward. These operations appear intended to be idempotent; no behavior change is recommended without a focused test and CI evidence.

### 2. Trading and market-data flow

**Verified source path:** `src/engine/orchestrator.py`, `src/engine/signal_engine.py`, `src/data/fetcher.py`, `src/data/quality_gate.py`, `src/features/pipeline.py`.  
**Deciding registry entries:** `DATA-001`–`DATA-006`, `INV-008`, `MODL-002`, `MODL-003`.

- The orchestrator owns one `SignalEngine` per configured active timeframe and warms model prerequisites before starting timeframe loops.
- Signal processing obtains the bar window, resolves the previous outcome tracker, builds one feature matrix off the event loop, rejects an empty feature matrix, then continues through model inference and the risk-gated signal path.
- `DataQualityGate` has explicit staleness and quality checks; freshness budgets are a declared type, not inferred from a timestamp alone. Exact provider failure semantics remain caller-specific; do not generalize all provider failures to fail-open.
- Training/inference feature alignment and no-lookahead are separately covered by model tests and registry entries. This source audit did not execute the tests or establish model quality in a live environment.

### 3. Online learner — previous open question resolved

**Verified source path:** `src/engine/orchestrator.py` around online-trainer construction and shutdown persistence; `src/engine/signal_engine.py::_learn_online`, `_record_p_long_for_bar`, and `_blend_online`; `src/models/online_trainer.py`.  
**Related tests:** `tests/test_online_trainer.py`, `tests/test_orchestrator_residual_arms.py`, plus signal-engine coverage.  
**Finding:** the online learner is wired, not merely imported. The orchestrator attempts one `OnlineTrainer` per active timeframe and injects it into that timeframe's `SignalEngine`; initialization failure degrades the optional component rather than preventing the core direction/meta engine from starting. Each feature-matrix tick calls `_learn_online`; the function only learns a new resolved label once per bar timestamp and catches failures best-effort. The batch direction probability is retained by bar for online meta training; online blending is applied to direction before downstream gates and to meta probability after meta prediction. Shutdown attempts to persist each learner.  
**Limit:** source wiring does not prove every learner initializes, warms up, or contributes nonzero blend weight in a particular deployment. The online component is optional and the orchestrator reports its absence as degradation.

### 4. Risk and position sizing

**Verified source path:** `src/risk/gates.py::evaluate_all_gates`, `src/risk/capital_preservation_floor.py`, `src/risk/kelly.py`, and the signal engine's sizing path.  
**Deciding registry entries:** `INV-001`, `INV-002`, `INV-004`, `INV-005`, `INV-008`, `MODL-002`.

- The gate sequence is ordered and returns on the first failed gate. The capital-preservation floor is an explicit veto; non-finite notional/capital is rejected before percentage comparison; disabled trading mode and maximum-notional checks have dedicated requirements/tests.
- Position sizing and advisory reductions are not substitutes for authoritative gates. Do not reorder gates, treat a veto as a soft penalty, or infer that a successful sizing calculation alone authorizes an order.
- This audit verified source/test declarations, not the current live configuration or the numerical performance of the risk model.

### 5. Execution, order state, and recovery

**Verified source path:** `src/execution/order_fsm.py`, `src/execution/exchange_contract.py`, `src/execution/order_manager.py`, `src/execution/live.py`, `src/runtime/reconcile.py`.  
**Deciding registry entries:** `INV-003`, `INV-007`, `INV-009`, `EXEC-001`–`EXEC-006`, `RES-001`, `RES-002`.

- The order FSM transition table is a read-only mapping; terminal states have no outgoing transitions. Exchange responses pass through a declared contract; an unknown status is not a valid fill transition.
- Order submission requires an idempotency key, reserves it before the exchange request, and persists order/FSM state for reconciliation.
- Live executor startup compares local positions with exchange holdings and blocks new entries while reconciliation discrepancies are unresolved. The reconciliation pass itself does not place/cancel orders.
- Recovery behavior is covered by dedicated FSM, idempotency, contract, and crash-replay tests. Those tests were not executed during this audit.

### 6. Runtime reconciliation

**Verified source path:** `src/runtime/production.py::RuntimeLoop`, `src/api/main.py::lifespan`, and runtime tests under `tests/runtime/`.  
**Deciding registry entries:** `RES-009`–`RES-025`.

- The production loop rediscovers components, synchronizes the registry, reconciles through the change manager, and flushes state repeatedly. Per-pass exceptions are logged and the control loop continues; this is deliberately isolated from the trading loop.
- The API lifespan starts the runtime loop before the orchestrator's first tick so persisted desired state can be reconciled before trading begins.
- Runtime components have explicit identity/capabilities and the registry states that some adapters are observe-only. An endpoint or registered component does not imply arbitrary runtime mutation authority.

### 7. API, WebSocket, and authorization

**Verified source path:** `src/api/auth.py`, `src/api/access_control.py`, `src/api/main.py`, `src/api/readiness.py`, `src/terminal/auth.py`.  
**Deciding registry entries:** `API-001`–`API-009`, `SECR-001`–`SECR-010`, `SECR-019`.

- The REST auth function validates configured strong keys and resolves primary/read-only roles; invalid server-side key configuration returns a service-unavailable response rather than opening the route. Route permission checks and operator second-factor checks are distinct.
- WebSocket auth supports the header and a browser query-parameter fallback. Query keys have greater access-log exposure than headers; preserve that caveat and never treat authentication as operator approval.
- The terminal service is a separate local process/authority with its own origin/host/token or Unix peer checks; terminal credentials are not interchangeable with trading API credentials.
- `API_REFERENCE.md` is a route-navigation index, not proof that the authorization matrix is correct for every endpoint. Route changes require tracing dependencies and role tests.

### 8. SQLite / Timescale storage parity

**Verified source path:** `src/data/storage.py` and `src/data/timescale_storage.py`; schema and migration tests are listed in the quality registry.  
**Finding:** both backends implement a shared logical contract, but physical schemas are not byte-identical: SQLite uses `INTEGER/REAL` and its own migration runner; Timescale uses `BIGINT/DOUBLE PRECISION/TIMESTAMPTZ` and a separate schema-version table. SQLite's base `trades` DDL intentionally omits columns added by later migrations to avoid duplicate-column failures on fresh databases; the Timescale DDL may declare the resulting columns directly.  
**Limit:** this source pass did not run both backends or exhaustively compare every query, index, constraint, and migration. Claim logical intent, not complete physical parity, until CI-backed contract coverage proves it.

### 9. Frontend, Electron, terminal, and process ownership

**Verified source path:** `frontend/src/desktop/terminalBridge.test.js`, `frontend/src/terminal/client.js`, `src/terminal/{server,auth,protocol,sessions}.py`, Electron configuration and terminal tests.  
**Finding:** the terminal is a distinct local PTY service; browser clients use a loopback WebSocket, while Electron uses a narrow IPC bridge. The renderer is designed around context isolation and no renderer Node integration; the server validates messages before they reach a PTY. Frontend process/terminal status is not the same as exchange/execution readiness.  
**Limit:** source and test presence do not prove host-specific permissions, process cleanup, or packaging behavior on this machine without the authorized CI/operational route.

### 10. E-01 through E-18 specialist-engine seam

**Verified source path:** `src/engines/orchestrator.py`; deciding test `tests/test_engine_seam_contract.py`; related integration audit `docs/runtime/INTEGRATION_AUDIT.md`.

| ID | Module / declared role | Load-bearing observation |
|---|---|---|
| E-01 | `e01_statistical.py` — statistical/HMM/ARIMA | Statistical regime/direction input |
| E-02 | `e02_microstructure.py` — order-book imbalance/microstructure | Reads order-book context |
| E-03 | `e03_information_theory.py` — entropy measures | Entropy influences confidence/consensus context |
| E-04 | `e04_fourier.py` — Fourier cycles/halving patterns | Requires block-height context for halving features |
| E-05 | `e05_onchain.py` — on-chain graph/wallet flows | Optional external on-chain/supply providers |
| E-06 | `e06_fractal.py` — Hurst/fractal/chaos | Regime/trend characterization |
| E-07 | `e07_linear_algebra.py` — PCA/cointegration | Trend and spread signals |
| E-08 | `e08_topology.py` — persistent homology | Optional topology dependency/fallback |
| E-09 | `e09_ml_meta.py` — ML meta-engine | Consumes earlier-engine confidence features |
| E-10 | `e10_supply.py` — protocol supply/stock-to-flow | Uses block-height context and asset-specific assumptions |
| E-11 | `e11_stochastic.py` — volatility/GBM/Heston/Merton | Supplies jump probability and volatility inputs to risk quantification |
| E-12 | `e12_options.py` — options market | Optional options-chain context; can abstain for unsupported assets |
| E-13 | `e13_contagion.py` — cross-asset contagion | Requires macro/cross-asset context |
| E-14 | `e14_sentiment.py` — sentiment | Requires sentiment provider context |
| E-15 | `e15_rl.py` — reinforcement learning | Secondary/sanity-check signal, not execution authority |
| E-16 | `e16_adversarial.py` — adversarial/manipulation detection | A manipulation veto input to the signal gate; preserve fail-safe semantics |
| E-17 | `e17_liquidity.py` — liquidity stress | Supplies liquidity stress input to risk quantification |
| E-18 | `e18_network.py` — network centrality/exchange flows | Requires exchange-flow graph context and should abstain when absent |

The module headers define these intended roles; they do not independently validate algorithm quality, provider freshness, or real-world predictive value.

- The 18 engines are associated to E-01…E-18 by positional order. `zip(..., strict=True)` enforces the expected list length; reordering is a behavior/attribution change even if all engines still execute.
- Each engine call is time-bounded and failures are represented as per-engine failures, then omitted from consensus. The pipeline separately derives risk context from E-11 and E-17 and preserves the E-16 manipulation-veto role. Engine failures must not be misdescribed as universally harmless: a missing engine can remove a required signal/veto and downstream risk gates must remain authoritative.
- The legacy integration audit is a historical change record with its own `main`/PR baseline, not a current snapshot of this branch. Use current source/tests for present behavior.

## Knowledge-pack/index audit

- `FILE_INDEX.md` inventories tracked application/repository paths and includes current working-tree hashes for modified tracked files; it always excludes codebase-analysis-docs/ to avoid self-referential hashes and also excludes other untracked analysis/build artifacts. Duplicate rows can occur because a path appears in more than one priority section.
<<<<<<< HEAD
- Final static revalidation found 1,243 FILE_INDEX rows / 1,193 unique tracked paths with zero missing paths or hash/line-count mismatches; 47 distinct source paths and 1,823 anchors in `CHUNK_INDEX.md` with zero metadata/hash/range issues; 19 stable source anchors in `CODEBASE_KNOWLEDGE.md` with zero hash/range issues; and 68 API decorator routes matching source inspection. These are point-in-time integrity results, not semantic completeness.
- The current source snapshot is recorded as commit `9128e3d...`; source changes after this audit require revalidation of affected hashes/ranges.
- Five Mermaid `.mmd` source files exist. `mmdc`/Mermaid CLI is not installed in the observed environment and no repository rendering workflow was found in the targeted workflow search. Diagram rendering is **blocked/unverified**, not passed.
- No prior deterministic refresh command was found in the targeted `scripts/`, `.claude/skills/`, and workflow search. Phase 3 now adds `scripts/codebase_knowledge_index.py`: CI `--check` validates tracked-file coverage, FILE_INDEX metadata, source-anchor hashes/ranges, and API_REFERENCE method/path/handler/line rows against the current FastAPI AST; `--refresh-file-index` rebuilds tracked-path rows, SHA8/line-count metadata, totals, and row numbers while preserving existing notes/priorities and never silently relabeling semantic anchor ranges. This is a bounded metadata refresher and integrity checker, not a generator for semantic CHUNK_INDEX content, API_REFERENCE content, or Mermaid diagrams. Those remain manually curated/unresolved.
=======
- At original knowledge-pack generation from baseline `9128e3d...`, static revalidation found 1,243 FILE_INDEX rows / 1,193 unique tracked paths with zero missing paths or hash/line-count mismatches; 47 distinct source paths and 1,823 anchors in `CHUNK_INDEX.md` with zero metadata/hash/range issues; 19 stable source anchors in `CODEBASE_KNOWLEDGE.md` with zero hash/range issues; and 68 API decorator routes matching source inspection. These are historical point-in-time results, not current checker output or semantic-completeness proof. FILE_INDEX has since been refreshed against the current PR worktree, including the new index tool and CI workflow changes. The first compact CI notice exposed stale CHUNK_INDEX hashes for `.github/workflows/ci.yml` after the new steps were added; its hash, total line count, and slice ranges have now been updated to the current 402-line file. The checker has not yet returned a fresh verdict on that correction.
- The current source snapshot is recorded as commit `9128e3d...`; source changes after this audit require revalidation of affected hashes/ranges.
- Five Mermaid `.mmd` source files exist. `mmdc`/Mermaid CLI is not installed in the observed local environment. A pinned Mermaid CLI render step has now been added to the existing CI lint/governance job, but rendering remains **unverified until GitHub Actions reports its result**.
- Phase 3 follow-on implementation in this PR adds `scripts/codebase_knowledge_index.py`. `--refresh-file-index` deterministically refreshes tracked-path rows and metadata while preserving existing priorities/notes; `--check` verifies tracked-file coverage, FILE_INDEX metadata, source-anchor hashes/ranges, and API route rows against the FastAPI AST. It intentionally does not generate semantic CHUNK_INDEX/API_REFERENCE content. Mermaid rendering is separately configured in the existing CI job. The worker-orchestrator chunk ranges/hash were reconciled manually to the main source delta; checker and diagram-render CI evidence remain pending.
>>>>>>> origin/main
- The tracked documentation `docs/quality/REQUIREMENTS_TRACEABILITY.md` is explicitly generated from `config/quality_registry.json`; do not hand-edit it. `STATE_BLOCKS.md` is a manually maintained handoff log. The Mermaid files are source diagrams; their render/export ownership is not established.

## Confirmed discrepancies and disposition

| ID | Finding | Classification | Disposition |
|---|---|---|---|
| AUD-01 | Knowledge master left online trainer activation unresolved despite constructor injection, per-tick call sites, blending, and shutdown persistence being directly traceable. | Verified documentation inaccuracy | Updated the master and recorded wiring with its optional-component limitation. |
| AUD-02 | The runtime integration audit carries a historical `main` / PR #431 baseline. | Historical artifact, not a current-state defect | Retain as historical evidence; use current source for current behavior. |
<<<<<<< HEAD
| AUD-03 | No prior index refresh utility existed; semantic chunk/API generation and Mermaid rendering remain unresolved. | Partially resolved tooling gap | Added deterministic FILE_INDEX refresh and CI integrity validation; semantic CHUNK_INDEX/API_REFERENCE regeneration and Mermaid rendering remain blocked/unverified. |
=======
| AUD-03 | No prior index refresh utility existed; semantic chunk/API generation and Mermaid rendering remain unresolved. | Partially resolved tooling gap | Added deterministic FILE_INDEX refresh, CI integrity validation, and CI Mermaid rendering; semantic CHUNK_INDEX/API_REFERENCE regeneration remains manual. CI verification of the new checker and diagram renderer is pending. |
>>>>>>> origin/main
| AUD-04 | Lifespan repeats some WebSocket task cleanup operations after its `finally` cleanup. | Low-severity maintenance observation; correctness impact unproven | No code change in this audit; require a focused regression test and CI evidence before simplifying lifecycle behavior. |
| AUD-05 | Full physical SQLite/Timescale parity and all 18 engines' deployment-time health cannot be certified from source inspection alone. | Verification limitation | Record as unverified; do not claim runtime/production readiness. |
| AUD-06 | Terminal architecture page's heading made its `bbe46cb6` pre-implementation table look current. | Verified documentation inaccuracy | Reworded the introduction and section heading to identify the table as a historical baseline while preserving design history. |

## Phase 2 outcome

<<<<<<< HEAD
The source-backed subsystem audit is documented. The verified online-learning description is corrected in the master knowledge reference; the terminal architecture document now labels its old pre-terminal state table as a historical baseline, and affected file/chunk index hashes and ranges were refreshed. No trading behavior, risk limit, order path, security guard, or database migration was changed because this audit did not establish a specific behavior defect that justified a production-code change. No tests/gates/CI/runtime checks were run. The FILE_INDEX refresher/checker is now implemented in Phase 3; semantic CHUNK_INDEX/API_REFERENCE regeneration and Mermaid rendering remain blocked/unverified. Follow-on defect implementation must use the repository's dependency-ordered gap register and the required sequence: diagnosis → root cause → correction → targeted verification → CI evidence → completion.
=======
The source-backed subsystem audit is documented. The verified online-learning description is corrected in the master knowledge reference; the terminal architecture document now labels its old pre-terminal state table as a historical baseline; FILE_INDEX metadata was refreshed; and a deterministic index refresher/checker plus a CI gate were added as follow-on governance tooling. No trading behavior, risk limit, order path, security guard, or database migration was changed because this audit did not establish a specific behavior defect that justified a production-code change. Tests and CI verification remain pending. Semantic CHUNK_INDEX/API_REFERENCE regeneration remains follow-on work; Mermaid rendering is CI-configured but unverified. Follow-on defect implementation must use the repository's dependency-ordered gap register and the required sequence: diagnosis → root cause → correction → targeted verification → CI evidence → completion.


## Supplemental source delta review through main `6232016c94db2025e9afddf4aa3e3e2331dc7b2f`

The source diff from the audited baseline `9128e3da8b9542efef5c89706b8f2e8f3512f054` to current main changes two application modules relevant to the knowledge pack:

- `src/models/horizon_learning.py`: schema migration now delegates missing-column additions to `_ensure_column`. It suppresses only SQLite duplicate-column errors that a fresh `PRAGMA table_info` confirms were resolved by a concurrent process; unrelated operational errors are re-raised. `tests/test_horizon_mlp.py` adds a simulated migration race. The test was not executed here.
- `src/workers/orchestrator.py`: worker SIGTERM now raises `SystemExit(0)` through `_handle_worker_sigterm` instead of calling the site-dependent `exit()` helper. `tests/test_workers_orchestrator.py` adds a direct handler contract assertion. The test was not executed here.

The API route source `src/api/main.py` did not change between those two commits, so the 68-route reference remains tied to that source snapshot. The `src/workers/orchestrator.py` CHUNK_INDEX section was updated to the current hash and its five-line insertion was reflected in subsequent ranges; other semantic anchors remain manually maintained. The current FILE_INDEX hashes identify the current source bytes. The new checker is intended to flag any remaining hash/range drift; CI evidence is pending. This delta review is not a substitute for the unrun CI suite.
>>>>>>> origin/main
