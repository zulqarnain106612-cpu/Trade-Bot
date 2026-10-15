# State Blocks

**Index version:** 1.0.1  
**Snapshot HEAD:** 9128e3da8b9542efef5c89706b8f2e8f3512f054  
**Status:** source analysis and documentation generation complete; intended changes are limited to codebase-analysis-docs.

## Phase 1 — Initial context scan

- **INDEX_VERSION:** 1.0.1
- **FILE_MAP_SUMMARY:** README.md, AGENTS.md, CLAUDE.md, pyproject.toml, runtime/optional requirements, src/api/__main__.py, src/api/main.py, src/config.py, src/engine/orchestrator.py, src/engine/signal_engine.py, frontend/src/App.jsx, frontend/src/hooks/useApi.js, Dockerfile, docker-compose.yml. Full inventory: codebase-analysis-docs/FILE_INDEX.md.
- **Findings:** async Python crypto-trading bot with FastAPI, React/Vite/Electron, SQLite/Timescale, terminal daemon, runtime control, optional horizon/ECC analytics and developer RAG/graph/tooling.
- **OPEN_QUESTIONS:** deployment environment/config differs from code defaults; live credentials/state were neither inspected nor inferred.
- **KNOWN_RISKS:** some existing docs are stale; API and headless worker each own a core loop if run independently against one account.
- **GLOSSARY_DELTA:** Orchestrator, SignalEngine, Bar, SignalResult, horizon, PTY, RAG.
- **NEXT_STEPS:** trace trade path, risk/execution, persistence, API security and runtime capabilities.

## Phase 2 — Architecture deep dive

- **INDEX_VERSION:** 1.0.1
- **FILE_MAP_SUMMARY:** src/api/main.py, src/engine/orchestrator.py, src/engine/signal_engine.py, src/data/fetcher.py, src/data/storage.py, src/data/timescale_storage.py, src/eventbus/, src/diagnostics/, src/runtime/, src/terminal/, frontend/src/, frontend/electron/.
- **Findings:** FastAPI lifespan is API composition root. Trading loop, runtime reconcile, event fanout, optional tuning/intelligence and per-user terminal are distinct concerns/processes.
- **OPEN_QUESTIONS:** deployed reverse-proxy policy and production database configuration not observed; provider fallbacks need per-provider trace before modification.
- **KNOWN_RISKS:** duplicate core loops, incomplete task/subscription cleanup, WebSocket query-key exposure, SQLite/Timescale migration drift, conflation of readiness and liveness.
- **GLOSSARY_DELTA:** runtime desired state, capability set, reconciler, unified ledger, live gate.
- **NEXT_STEPS:** map business features to callers, state stores and deciding tests.

## Phase 3 — Feature analysis

- **INDEX_VERSION:** 1.0.1
- **FILE_MAP_SUMMARY:** src/features/pipeline.py, src/models/trainer.py, src/models/model_registry.py, src/regime/, src/risk/gates.py, src/execution/, src/strategies/, src/tuning/, src/intelligence/, src/engines/, src/ecc/, src/agent_control/.
- **Findings:** market data → feature/model/regime → risk/position sizing → execution/ledger/audit is the core path; strategy and horizon/ECC systems depend on explicit wiring/config.
- **OPEN_QUESTIONS:** README’s online-learning status conflicts with current SignalEngine symbols; call conditions need tracing before claiming active per-tick use.
- **KNOWN_RISKS:** training/inference feature-order mismatch, model-pair split promotion, label leakage, gate-order changes, unstable strategy IDs, unproven provider fallback assumptions.
- **GLOSSARY_DELTA:** meta-label, triple barrier, CPCV/OOS, shadow model, promotion gauntlet, ECC analytics.
- **NEXT_STEPS:** record behavior gotchas, DB/API contracts and source anchors.

## Phase 4 — Nuances and gotchas

- **INDEX_VERSION:** 1.0.1
- **FILE_MAP_SUMMARY:** CLAUDE.md, config/command_policy.json, config/architecture_layers.json, config/quality_registry.json, config/math_registry.json, src/config.py, src/risk/gates.py, src/execution/order_fsm.py, docs/runtime/RUNTIME_PLATFORM.md, docs/terminal/ARCHITECTURE.md.
- **Findings:** risk field units differ by domain; SQLite columns may be migration-owned; runtime adapters are not universally mutable; E-01…E-18 wiring is positional; local quality checks and direct CI data access are policy-restricted.
- **OPEN_QUESTIONS:** online trainer activation path; live host readiness; exact physical parity of all Timescale/SQLite types and indexes outside inspected slices.
- **KNOWN_RISKS:** contradictory docs; unobserved environment overrides; Linux-specific terminal assumptions.
- **GLOSSARY_DELTA:** desired vs actual state, shadow tuning, idempotency key, fail-closed.
- **NEXT_STEPS:** finish route/schema/glossary/file-range indexes.

## Phase 5 — Technical reference

- **INDEX_VERSION:** 1.0.1
- **FILE_MAP_SUMMARY:** src/api/auth.py, src/api/access_control.py, src/api/main.py, src/data/storage.py, src/data/timescale_storage.py, src/runtime/contracts.py, src/runtime/registry.py, src/terminal/protocol.py, pyproject.toml, .github/workflows/ci.yml.
- **Findings:** API routes AST-indexed; schema identities and logical relations documented; API auth roles and engineering-policy boundaries summarized; glossary and line/hash anchors generated.
- **OPEN_QUESTIONS:** handler signatures alone cannot establish effective authorization; read the active dependencies/branch for each route before modifying it.
- **KNOWN_RISKS:** anchors/hashes become stale after source edits.
- **GLOSSARY_DELTA:** DB identities, API role, capability set, coverage floor, command effect.
- **NEXT_STEPS:** assemble and verify master plus supplemental files.

## Phase 6 — Final assembly

- **INDEX_VERSION:** 1.0.1
- **FILE_MAP_SUMMARY:** codebase-analysis-docs/CODEBASE_KNOWLEDGE.md, FILE_INDEX.md, CHUNK_INDEX.md, API_REFERENCE.md, STATE_BLOCKS.md and five Mermaid files in codebase-analysis-docs/assets/.
- **Findings:** documentation deliverables generated under the requested folder. Code/source behavior was not intentionally modified.
- **OPEN_QUESTIONS:** tests and quality gates were not run because repo policy blocks local checks; no CI result, live exchange connection, account state or local secrets were inspected.
- **KNOWN_RISKS:** describes local checkout snapshot, not future/remote HEAD; some auxiliary code is inventoried by AST rather than exhaustively read.
- **GLOSSARY_DELTA:** codebase-analysis-docs/CODEBASE_KNOWLEDGE.md section 4.5.
- **NEXT_STEPS:** review the generated index files; regenerate source hashes/ranges after later changes.

## Continuation state

- **Master:** codebase-analysis-docs/CODEBASE_KNOWLEDGE.md
- **Indexes:** codebase-analysis-docs/FILE_INDEX.md, codebase-analysis-docs/CHUNK_INDEX.md, codebase-analysis-docs/API_REFERENCE.md
- **Diagrams:** codebase-analysis-docs/assets/architecture.mmd, trading-flow.mmd, runtime-change.mmd, schema-er.mmd, terminal-architecture.mmd
- **Pre-existing user state to preserve:** .claude/launch.json was modified; .vscode/launch.json and build/ were untracked before this documentation task.
- **Validation boundary:** no tests/build/lint/security/quality-gate was run. For operational CI evidence, use only the project's approved compact current-HEAD notice route and its policy.


## 2026-10-13 continuation — knowledge integration checkpoint

- **Baseline:** branch `codex/adaptive-horizon-intelligence`; HEAD `9128e3da8b9542efef5c89706b8f2e8f3512f054`; upstream `origin/codex/adaptive-horizon-intelligence`.
- **Protected pre-existing paths:** `.claude/launch.json` (modified), `.vscode/launch.json` (untracked), `build/` (untracked), and this untracked knowledge pack. Do not reset, clean, or overwrite these paths.
- **Instruction change:** root `AGENTS.md` now matches the CI-only validation policy and routes agents to task-specific knowledge sections. This is a working-tree change, not committed or CI-validated.
- **Index integrity evidence:** 1,823 chunk anchors checked against current source: no missing files, hash mismatches, or out-of-range line ranges. Nineteen core knowledge anchors also matched. API reference route count (68) matches textual decorator count in `src/api/main.py`.
- **Index correction:** both copies of the `FILE_INDEX.md` priority table now reflect the current `AGENTS.md` line count/hash and current `.github/workflows/ci.yml` line count. Other index rows were not fully revalidated.
- **Diagram limitation:** all five `.mmd` files exist and declare Mermaid diagram types; Mermaid CLI is unavailable, so rendering has not been verified.
- **Policy:** `CLAUDE.md` and `.claude/skills/local-checks/SKILL.md` prohibit local test/build/lint/type/security/quality-gate execution. No such commands were run. GitHub Actions evidence is unavailable for this working-tree change because it has not been pushed to an open PR; do not fetch raw CI records/logs to bypass the sanctioned notice channel.
- **Unresolved:** no reproducible codebase-index generator was identified in targeted searches; semantic review of all architecture/security/trading claims remains incomplete; runtime and CI defects have not been established solely from this pass.
- **Next steps:** identify whether an existing supported generator exists outside the searched paths; if not, design a non-destructive deterministic refresh/check command with regression coverage and CI integration only after inspecting relevant policy/registry structure. Continue source-backed subsystem audit and fix only confirmed defects.


## 2026-10-13 — Phase 1 live baseline verification

**Status: complete (read-only baseline and knowledge-pack integrity; implementation phases not started by this checkpoint).**

- Verified checkout: branch `codex/adaptive-horizon-intelligence`; HEAD `9128e3da8b9542efef5c89706b8f2e8f3512f054`; upstream `origin/codex/adaptive-horizon-intelligence`; origin is `git@github.com:zulqarnain106612-cpu/Trade-Bot.git`; HEAD and upstream are equal (0 ahead / 0 behind).
- No active merge, cherry-pick, revert, rebase, or index-lock marker was present. Latest five commits were inspected. GitHub search found no open PRs in `zulqarnain106612-cpu/Trade-Bot` at inspection time. No raw CI run/check data was queried.
- Preserved pre-existing workspace paths: modified `.claude/launch.json`; untracked `.vscode/launch.json`, `build/`, and `codebase-analysis-docs/`. `AGENTS.md` is also modified by the earlier knowledge-integration work; it remains uncommitted. No staging, commit, push, branch switch, or history mutation was performed.
- Re-read `AGENTS.md`, `CLAUDE.md`, `config/command_policy.json`, relevant quality-registry references, `scripts/local_checks.py`, and RAG/KG ingestion scripts. Authoritative constraint remains: GitHub Actions is the only route for tests/build/lint/type/security/quality gates; only the exact permitted read-only `python3 scripts/local_checks.py prepare` route can expose the current HEAD's SHA-bound CI notice when its preconditions are met. No tests or gates were run.
- Knowledge pack inventory: five Markdown documents and five Mermaid source diagrams are present. `FILE_INDEX.md`: 1,243 index rows representing 1,193 unique tracked paths, 0 missing paths/hash/line-count mismatches. `CHUNK_INDEX.md`: 47 distinct indexed source paths, 0 stale hash/line-count entries; 1,823 source anchors, 0 hash/range issues. `CODEBASE_KNOWLEDGE.md`: 19 stable source anchors, 0 hash/range issues. API route inventory records 68 decorators and source inspection count is 68. Mermaid CLI is unavailable, so diagram rendering is not verified.
- Existing index scope caveat: the knowledge snapshot is based on the current HEAD, while `FILE_INDEX.md` documents working-tree hashes for modified tracked files. Untracked analysis files and build outputs are excluded from the tracked-file inventory by design. Static anchor checks establish path/hash/range integrity, not semantic completeness or runtime correctness.
- Next: proceed to Phase 2 source-backed subsystem audit using current source and tests as authorities; classify confirmed gaps separately from hypotheses, then implement only evidence-backed fixes with policy-required regression coverage and CI evidence.


## 2026-10-13 — Phase 2 source-backed subsystem audit

- **Scope:** startup/shutdown/readiness; trading and market-data flow; freshness/features/model alignment; online learner wiring; risk/sizing; execution FSM and recovery; runtime reconciliation; API/WebSocket authorization; SQLite/Timescale schema strategy; frontend/Electron/terminal ownership; E-01…E-18 engine seam.
- **Source-backed audit:** see `codebase-analysis-docs/PHASE_2_SOURCE_AUDIT.md`. Current source and named tests/registry entries were traced for each subsystem. The audit distinguishes source facts from deployment/runtime claims and records six findings.
- **Verified documentation corrections:** online training is wired per active timeframe: the orchestrator constructs/injects an optional trainer; `SignalEngine` learns from resolved labels and blends online direction/meta outputs; shutdown attempts persistence. The terminal architecture document now marks its `bbe46cb6` pre-terminal table as historical, and the affected file/chunk index entries were refreshed. This corrects the prior unresolved item. It does not prove the learner warmed up or contributed nonzero weight in a live deployment.
- **Other findings:** the legacy `docs/runtime/INTEGRATION_AUDIT.md` is historical (its baseline is main/PR #431); lifespan repeats some WebSocket task cleanup after the `finally` cleanup (maintenance observation only; no behavior change); physical SQLite/Timescale parity and deployment health remain unverified.
- **Index state:** final static revalidation found 1,243 FILE_INDEX rows / 1,193 unique tracked paths with zero missing paths or hash/line-count mismatches; 47 distinct CHUNK_INDEX source paths and 1,823 anchors with zero metadata/hash/range issues; 19 stable knowledge anchors; and 68 API decorators matching the route inventory. These are point-in-time structural checks, not semantic completeness.
- **Refreshability/rendering:** added `scripts/codebase_knowledge_index.py` with a CI integrity check and a deterministic FILE_INDEX rebuild that preserves existing human notes/priorities and refuses tracked paths missing from the working tree. It does not regenerate semantic CHUNK_INDEX anchors, API_REFERENCE, or Mermaid diagrams. Mermaid CLI/render workflow remains unavailable/unverified; no rendering success is claimed.
- **Validation policy:** no tests, builds, lint/type/security checks, quality gates, raw CI data, live exchange calls, or credentials were inspected. The approved local diagnostic remains the exact SHA-bound `python3 scripts/local_checks.py prepare` command only when preconditions are met.
- **Protected workspace:** `.claude/launch.json`, `.vscode/launch.json`, `build/`, the pre-existing untracked knowledge pack, and the `AGENTS.md` working-tree change remain preserved. No staging, commit, push, branch switch, or history mutation was performed.
- **Phase 2 disposition:** source-backed audit and verified knowledge corrections are complete. No production behavior change was justified by the audited evidence. Phase 3 now addresses FILE_INDEX refresh/check ownership; semantic chunk/API generation and Mermaid rendering remain unresolved.


## 2026-10-13 — Phase 3 knowledge tooling and instruction integration

- **Implementation:** added scripts/codebase_knowledge_index.py with deterministic --refresh-file-index and read-only --check modes. Refresh rebuilds tracked-path rows, row numbers, totals, SHA8/line counts while preserving existing priority labels and notes; newly tracked paths get conservative defaults. The knowledge-pack directory is excluded even if committed, preventing self-referential index hashes.
- **CI wiring:** .github/workflows/ci.yml now runs the index integrity checker alongside generated-document checks. It checks tracked-path coverage and FILE_INDEX freshness, CHUNK_INDEX/CODEBASE_KNOWLEDGE anchor hashes and line bounds, and AST-derived API route method/path/handler/line parity against API_REFERENCE.md; it refuses to silently bless stale semantic ranges.
- **Regression coverage:** added tests/test_codebase_knowledge_index.py for metadata refresh, missing tracked files, stale/out-of-range anchors, and API route inventory drift; registered GOV-066 in config/quality_registry.json and regenerated docs/quality/REQUIREMENTS_TRACEABILITY.md.
- **Instructions:** AGENTS.md now directs agents to the CI check and distinguishes safe FILE_INDEX refresh from deliberate semantic-anchor maintenance.
- **Index refresh:** ran the explicit python3 scripts/codebase_knowledge_index.py --refresh-file-index generation command after staging only the two new implementation/test files. The index now lists 1,195 tracked application/repository files, including the new script/test; 997 are Python files. The knowledge-pack directory remains excluded by design.
- **Source-backed docs:** online learner wiring and terminal architecture historical-baseline scope are documented in the knowledge master and audit. The Mermaid sources remain unrendered; no renderer or renderer workflow was available in the inspected environment.
- **Validation status:** no test suite, lint/format, type/security gate, or CI run was executed locally. The added regression tests and CI step are pending authoritative GitHub Actions evidence. The --check mode was not run locally because repository policy prohibits local checks outside the exact SHA-bound scripts/local_checks.py prepare route.
- **Git state:** only scripts/codebase_knowledge_index.py and tests/test_codebase_knowledge_index.py are staged. Other implementation files are unstaged; user-owned .claude/launch.json, .vscode/launch.json, and build/ remain unstaged/untracked and untouched. No commit, push, branch switch, or merge occurred.
- **Remaining:** validate the new checker/tests in GitHub Actions through the sanctioned workflow, resolve any CI failures, then continue the remaining knowledge/API/diagram tooling and dependency-ordered engineering gaps. Do not mark the overall plan complete.

## Phase 4 — Knowledge integration handoff (2026-10-14)

- **Verified repository baseline:** `main` at `7dc29702a728bc18365bfc41afae1a8017d8878b`, which merged PR #456. The earlier `codex/adaptive-horizon-intelligence` checkout remains a separate, dirty user workspace and is not the delivery workspace for this follow-up.
- **Completed in this follow-up:** root `AGENTS.md` now routes agents to task-relevant knowledge references and explicitly treats the pack as navigation evidence rather than proof; `CODEBASE_KNOWLEDGE.md` distinguishes the audited source baseline from the documentation merge commit.
- **Phase 1/2 implementation status:** the knowledge files, FILE_INDEX refresh/check utility, CI index gate, and Mermaid rendering workflow are present on `main`. This does not establish that every index is current or that the CI checks passed.
- **Unverified / still open:** the repository policy permits CI diagnostics only through `python3 scripts/local_checks.py prepare` after an authorized push. Do not retrieve raw workflow logs, job records, artifacts, annotations, or check-run data. Semantic `CHUNK_INDEX.md` / `API_REFERENCE.md` regeneration remains manual; the checker detects drift but does not regenerate semantic anchors. The Phase 2 source audit remains a source-level review, not proof of test coverage, runtime correctness, security, database parity, or production readiness.
- **Next:** use the approved compact notice after the follow-up PR is pushed; fix only failures reported by that channel. Continue Phase D as evidence-backed subsystem reviews; do not implement speculative trading/runtime changes where no reproducible defect and deciding regression contract have been established.
