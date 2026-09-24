<!--
GENERATED FILE — DO NOT EDIT BY HAND.

Source of truth: config/quality_registry.json
Regenerate with: python3 scripts/generate_quality_docs.py
CI check:        python3 scripts/generate_quality_docs.py --check

Editing this file directly will be reverted by the next regeneration and will
fail CI. Change the registry instead; the registry is reviewed as a diff and
validated by src/quality/registry.py at load time.
-->

# Requirements traceability

Every requirement, trading invariant, regression and security regression this
project holds itself to, and the test that decides each one.

The chain this document exists to make auditable:

```
Requirement
   ↓
Implementation
   ↓
Test
   ↓
CI gate
   ↓
Release evidence
```

A row here is a claim about the tree, not an aspiration. The loader stats every
named test file and every named owning module, so a row cannot survive the
deletion of the thing it points at.

## How to read a status

**VERIFIED** — At least one named test file exists on disk and the entry is considered fully covered. No planned_in.

**PARTIAL** — Named tests exist and cover part of the statement. planned_in names the PR that finishes it. This is the honest status for a requirement the tree already touches but does not yet pin down.

**PLANNED** — No test exists yet. planned_in names the PR that will create one. Naming no test and no PR is not a permitted state.

**ACCEPTED GAP** — A deliberate, attributed, dated decision not to verify. Requires a waiver. Never available to a critical entry.

## Summary by status

| Status | Entries |
|---|---|
| VERIFIED | 115 |
| PARTIAL | 0 |
| PLANNED | 0 |
| ACCEPTED GAP | 0 |
| **Total** | **115** |

## Summary by subsystem

| Subsystem | Entries | Verified |
|---|---|---|
| Risk | 10 | 10 |
| Execution | 11 | 11 |
| Portfolio | 1 | 1 |
| Signal and features | 5 | 5 |
| Models and leakage | 7 | 7 |
| Data, money and time | 7 | 7 |
| API and WebSocket | 9 | 9 |
| Cryptography and secrets | 16 | 16 |
| Supply chain and artifacts | 7 | 7 |
| Resilience and recovery | 8 | 8 |
| Release and production | 9 | 9 |
| Governance | 25 | 25 |

## Outstanding work by phase

| Phase | Title | Entries |
|---|---|---|
| PR-001 | Quality/Security Foundation | — |
| PR-002 | Risk Invariants + Boundary Tests | — |
| PR-003 | Signal/Feature Verification | — |
| PR-004 | Model/Leakage Verification | — |
| PR-005 | Execution/FSM/Exchange Contracts | — |
| PR-006 | Regression + Property Testing | — |
| PR-007 | API/WebSocket Security | — |
| PR-008 | Cryptographic/Secret Architecture | — |
| PR-009 | Supply-Chain + Artifact Security | — |
| PR-010 | Recovery/Chaos/Performance | — |
| PR-011 | Paper-Trading Qualification | — |
| PR-012 | Production/Canary Security Gate | — |

---

## Risk

#### `INV-001` — No order exceeds the configured maximum notional

**VERIFIED** · critical · invariant · source: QE-42

For every order the system submits, notional <= the configured ceiling for that symbol and account, with no path that bypasses the check.

- **If violated:** A single mis-sized order puts more capital at risk than the risk policy permits.
- **Owned by:** `src/risk/gates.py`, `src/risk/kelly.py`
- **Depends on:** `RISK-001`
- **Verification:**
  - `tests/trading/invariants/test_inv_001_max_notional.py` (risk) — Pins the ceiling at the boundary, proves no otherwise-passing input talks the stack past it, and states the sizer-cap versus gate-authority relationship.
  - `tests/test_kelly_notional_cap.py` (risk) — Caps the Kelly sizer's notional.
  - `tests/test_cvar_notional_cap.py` (risk) — Caps the CVaR path.

#### `INV-002` — No new entry while a capital-preservation halt is active

**VERIFIED** · critical · invariant · source: QE-42

While the capital-preservation floor is breached, no new entry order may be produced by any strategy, engine or manual path.

- **If violated:** The bot keeps trading through the drawdown it was supposed to stop at.
- **Owned by:** `src/risk/capital_preservation_floor.py`, `src/risk/gates.py`
- **Verification:**
  - `tests/trading/invariants/test_inv_002_capital_preservation_halt.py` (risk) — Halts at the threshold, never auto-clears on recovery, only re-authorisation lifts it, and it is evaluated first so the audit trail names the right control.
  - `tests/test_capital_preservation_floor.py` (risk) — Covers the floor's own decision.

#### `INV-004` — NaN or Infinity cannot produce an executable order

**VERIFIED** · critical · invariant · source: QE-42

Any non-finite value reaching price, quantity, notional, confidence or a risk scalar results in refusal, never in an order.

- **If violated:** A NaN propagates through sizing and an order of undefined size is submitted.
- **Owned by:** `src/risk/gates.py`, `src/strategies/position_sizing.py`
- **Verification:**
  - `tests/trading/invariants/test_inv_004_non_finite.py` (property) — Walks NaN and both infinities through every sizer, every gate and the assembled stack, and pins the two documented fail-open exceptions.
  - `tests/test_gates_non_finite.py` (risk) — Covers the gate stack.

#### `INV-006` — Risk engine failure cannot result in an executable order

**VERIFIED** · critical · invariant · source: QE-42,QE-26

If the risk engine raises, times out or is unavailable, the decision is NO TRADE. There is no default-allow path.

- **If violated:** An exception in the safety layer silently removes the safety layer.
- **Owned by:** `src/risk/gates.py`
- **Depends on:** `GOV-004`
- **Verification:**
  - `tests/trading/invariants/test_inv_006_risk_engine_failure.py` (risk) — The drift gate fails closed on any exception; a raising gate propagates rather than becoming a pass; no except block in src/risk swallows into a default-allow.
  - `tests/test_risk_gates.py` (risk) — Covers gate outcomes.

#### `RISK-001` — Maximum position exposure must not exceed the configured ceiling

**VERIFIED** · critical · requirement · source: QE-5

For every symbol and for the portfolio as a whole, exposure stays at or below the configured ceiling under every sizing path, including manual overrides.

- **If violated:** One oversized position turns a normal drawdown into an account-ending one.
- **Owned by:** `src/strategies/position_sizing.py`, `src/risk/gates.py`
- **Verification:**
  - `tests/trading/invariants/test_inv_001_max_notional.py` (risk) — The composition: the sizer's cap is 25% of capital, the gate's ceiling is 5%, and the gate is the authority.
  - `tests/risk/test_risk_limit_boundaries.py` (risk) — Just inside, exactly on, and just outside the position-size limit.
  - `tests/test_position_sizing.py` (risk)
  - `tests/test_risk_gates.py` (risk)

#### `RISK-002` — Boundary values at every risk limit are exercised

**VERIFIED** · high · requirement · source: QE-86

Each configured risk limit has tests at limit-epsilon, limit, and limit+epsilon, so an off-by-one comparison cannot pass.

- **If violated:** A `<` written where `<=` was meant permits exactly the trade the limit exists to stop.
- **Owned by:** `src/risk/gates.py`
- **Verification:**
  - `tests/risk/test_risk_limit_boundaries.py` (risk) — Nine limits, three cases each, with the inclusive or exclusive side of every comparison named in the test name and tabulated in the module docstring.

#### `RISK-003` — Position size is non-negative and bounded

**VERIFIED** · critical · requirement · source: QE-50

For arbitrary generated inputs, the sizer returns a value in [0, limit] or refuses; it never returns a negative, non-finite or unbounded size.

- **If violated:** A negative size inverts the intended direction of the trade.
- **Owned by:** `src/strategies/position_sizing.py`, `src/risk/kelly.py`
- **Verification:**
  - `tests/property/test_position_sizing_properties.py` (property) — Seeded generation over pools weighted toward the values that break comparisons: finite, non-negative and under the ceiling for every sizer and for the combined recommendation.

#### `RISK-004` — Probabilities and confidences stay within [0, 1]

**VERIFIED** · high · requirement · source: QE-50

Every value the system treats as a probability is in [0, 1] where it is consumed, or is rejected at the boundary.

- **If violated:** A confidence of 4.0 multiplies position size by four.
- **Owned by:** `src/strategies/position_sizing.py`, `src/intelligence/calibration.py`
- **Verification:**
  - `tests/risk/test_probability_bounds.py` (property) — An out-of-range probability refuses rather than saturating to the largest permitted bet, and the Bayesian shrinkage primitive guarantees its own output is a probability.

#### `RISK-005` — No optimizer may directly modify a live risk control

**VERIFIED** · critical · requirement · source: QE-84

Self-tuning output reaches live risk parameters only through bounds, offline evaluation, out-of-sample check, risk tests, shadow, paper and an approval -- never directly.

- **If violated:** An optimizer relaxes the very limit that would have stopped its own bad idea.
- **Owned by:** `src/tuning/gate.py`, `src/tuning/promotion_gauntlet.py`, `src/tuning/live_overrides.py`
- **Verification:**
  - `tests/test_tuning_gate.py` (verification)
  - `tests/test_promotion_gauntlet.py` (verification)
  - `tests/qualification/test_self_tuning_firewall.py` (risk) — Bounds are checked before quality, so an excellent out-of-bounds proposal is refused before its improvement is weighed; a regression anywhere blocks promotion, and the optimizer cannot reach an executor.

#### `RISK-006` — Mutation score on the risk subsystem is at or above 90%

**VERIFIED** · high · requirement · source: QE-49

Nightly mutation testing of the risk modules kills at least 90% of generated mutants.

- **If violated:** High line coverage hides a suite that would not notice if a comparison flipped.
- **Owned by:** `config/mutation_thresholds.json`, `scripts/check_mutation_score.py`
- **Verification:**
  - `tests/regression/test_regression_registry_contract.py` (mutation) — The 90% risk and sizing floors are declared, every target exists on disk, a run with no mutants is not a pass, and a timeout counts against the score.

## Execution

#### `INV-003` — Unknown exchange order state cannot become FILLED without reconciliation

**VERIFIED** · critical · invariant · source: QE-42,QE-5

An order whose exchange status is unknown, missing or unparseable is never transitioned to FILLED; it enters a reconciliation state instead.

- **If violated:** The bot books a fill that never happened, and every downstream position figure is wrong.
- **Owned by:** `src/execution/exchange_contract.py`, `src/execution/order_fsm.py`
- **Depends on:** `EXEC-004`
- **Verification:**
  - `tests/contract/test_exchange_order_contract.py` (contract) — There is no mapping from UNKNOWN to any FSM state, so an unrecognised status cannot become FILLED however the caller is written.
  - `tests/execution/test_order_fsm_totality.py` (execution) — No terminal state can reach FILLED, and PENDING cannot jump past the FILLING confirmation step.
  - `tests/test_order_fsm_transition_table.py` (execution)
  - `tests/test_live_executor_fsm.py` (execution)

#### `INV-005` — A disabled trading mode cannot submit orders

**VERIFIED** · critical · invariant · source: QE-42

When live trading is disabled, no code path reaches the live exchange client, including scheduled jobs, retries and reconciliation.

- **If violated:** The bot trades real money while the operator believes it is in paper mode.
- **Owned by:** `src/execution/paper.py`, `src/risk/gates.py`
- **Verification:**
  - `tests/trading/invariants/test_inv_005_disabled_mode.py` (execution) — The live gate refuses live until both models validate, the paper executor has no import or attribute reaching a venue, and every non-paper mode is gated by default.
  - `tests/test_paper_only_timeframe_routing.py` (execution)

#### `INV-007` — Duplicate execution requests cannot create duplicate positions

**VERIFIED** · critical · invariant · source: QE-42,QE-58

Two execution requests carrying the same idempotency key produce at most one exchange order and one position change.

- **If violated:** A retry doubles the position and the account is twice as exposed as intended.
- **Owned by:** `src/execution/idempotency.py`
- **Depends on:** `EXEC-001`
- **Verification:**
  - `tests/execution/test_idempotency_end_to_end.py` (execution) — Deterministic keys that do not collide across intents, a registry that refuses a replay, and 25 racing reservations that yield exactly one submission.
  - `tests/test_idempotency.py` (execution)

#### `INV-009` — Position and account state after restart reconcile with the exchange

**VERIFIED** · critical · invariant · source: QE-42

After any restart, the reconstructed position and balance state matches the exchange's, or the system halts rather than trading on a guess.

- **If violated:** The bot restarts believing it is flat while holding a real position, and hedges nothing.
- **Owned by:** `src/execution/unified_ledger.py`, `src/diagnostics/disaster_recovery.py`
- **Verification:**
  - `tests/recovery/test_crash_replay.py` (recovery) — Reconstructed state is compared against the venue at every crash point; signed quantities, partial fills and dust are each distinguished from a missing position.

#### `EXEC-001` — Execution requests carry an idempotency key end to end

**VERIFIED** · critical · requirement · source: QE-58

Every execution request carries a key derived from its decision, and the executor refuses to act twice on the same key.

- **If violated:** A network retry becomes a second real order.
- **Owned by:** `src/execution/idempotency.py`, `src/execution/order_manager.py`
- **Verification:**
  - `tests/execution/test_idempotency_end_to_end.py` (execution) — The key travels to the venue as a client order id, and only a provably-unsent request releases it -- a timeout keeps it claimed.
  - `tests/test_idempotency.py` (execution)

#### `EXEC-002` — The order state machine is total and its transitions are legal

**VERIFIED** · critical · requirement · source: QE-90

Every (state, event) pair has a defined outcome, and no transition outside the declared table is reachable.

- **If violated:** An unhandled exchange event leaves an order in a state nothing knows how to close.
- **Owned by:** `src/execution/order_fsm.py`
- **Verification:**
  - `tests/execution/test_order_fsm_totality.py` (contract) — The entire cross product of states and targets, each cell either a permitted transition or a refusal that leaves the state untouched.
  - `tests/test_order_fsm_transition_table.py` (contract)
  - `tests/test_order_fsm.py` (execution)

#### `EXEC-003` — Exchange responses are validated against a declared contract

**VERIFIED** · critical · requirement · source: QE-90,QE-51

Every exchange response is parsed against a schema; unknown, missing or malformed fields produce an explicit failure, not a default.

- **If violated:** A changed exchange field silently reads as zero and the bot mis-books a fill.
- **Owned by:** `src/execution/exchange_contract.py`, `src/execution/order_manager.py`
- **Verification:**
  - `tests/contract/test_exchange_order_contract.py` (contract) — A total parse: every input yields an answer, and one the contract cannot vouch for reports needs_reconciliation rather than a plausible default.

#### `EXEC-004` — Unknown exchange order status must never be treated as FILLED

**VERIFIED** · critical · requirement · source: QE-5

An unrecognised status string maps to an explicit UNKNOWN outcome that triggers reconciliation, never to a terminal success.

- **If violated:** Position accounting diverges from the exchange without anything reporting an error.
- **Owned by:** `src/execution/exchange_contract.py`
- **Verification:**
  - `tests/contract/test_exchange_order_contract.py` (contract) — An unrecognised status maps to UNKNOWN, and an unknown status carrying an otherwise perfect fill still reconciles rather than booking it.
  - `tests/test_live_fsm_integration.py` (integration)

#### `EXEC-005` — The kill switch is authenticated, authorized, audited, idempotent and durable

**VERIFIED** · critical · requirement · source: QE-25

Activating the kill switch requires an operator role, writes an audit event, is safe to repeat, blocks new entries immediately, and survives restart.

- **If violated:** The control of last resort is unavailable exactly when it is needed.
- **Owned by:** `src/risk/strategy_kill_switch.py`, `src/execution/mode_persistence.py`
- **Verification:**
  - `tests/test_strategy_kill_switch.py` (component)
  - `tests/test_strategy_kill_switch_wiring.py` (integration)
  - `tests/api/test_kill_switch_durability.py` (security) — Durability: the halt is persisted atomically and restored at startup; a missing file means a first start, an unreadable one resolves to the most restrictive mode.
  - `tests/api/test_injection_and_rate_limiting.py` (api) — The halt as an API control: authenticated, authorized by role, and idempotent when repeated.

#### `EXEC-006` — Partial fills and fees are accounted exactly

**VERIFIED** · high · requirement · source: QE-81

Position, average price, fee and realised PnL after a sequence of partial fills match a hand-calculated fixture.

- **If violated:** PnL is wrong, so every downstream risk decision is made on false numbers.
- **Owned by:** `src/execution/order_fsm.py`, `src/execution/post_trade.py`
- **Verification:**
  - `tests/execution/test_venue_precision.py` (unit) — Hand-calculated volume-weighted averages, including the arithmetic mean stated explicitly as the wrong answer, and 200 fills checked against an independent Decimal accumulation.
  - `tests/test_order_manager_partial_fills.py` (unit)
  - `tests/test_executor_fee_accounting.py` (unit)

#### `EXEC-007` — Mutation score on the execution subsystem is at or above 90%

**VERIFIED** · high · requirement · source: QE-49

Nightly mutation testing of the execution modules kills at least 90% of generated mutants.

- **If violated:** The FSM tests assert shape without asserting behaviour.
- **Owned by:** `config/mutation_thresholds.json`, `.github/workflows`
- **Verification:**
  - `tests/regression/test_regression_registry_contract.py` (mutation) — The execution subsystem's 90% floor, mutating order_fsm, idempotency and exchange_contract nightly.

## Portfolio

#### `PORT-001` — Portfolio-level exposure, correlation and agreement limits are enforced

**VERIFIED** · high · requirement · source: QE-2,QE-42

Aggregate exposure, cross-strategy correlation and portfolio agreement are evaluated before an order is sized, and a breach reduces or refuses the order rather than being reported after the fact.

- **If violated:** Five uncorrelated-looking positions turn out to be one position in five costumes.
- **Owned by:** `src/risk/portfolio_correlation.py`, `src/risk/portfolio_agreement.py`
- **Verification:**
  - `tests/portfolio/test_portfolio_limits.py` (risk) — Both scalars asserted on the notional that reaches the sizer rather than on the number the tracker reports, and the small-sample shrinkage pinned as intended behaviour rather than discovered as a surprise.
  - `tests/test_portfolio_correlation.py` (risk)
  - `tests/test_portfolio_agreement.py` (risk)

## Signal and features

#### `SIG-001` — Golden signal fixtures pin end-to-end behaviour

**VERIFIED** · high · requirement · source: QE-43

Named fixtures carry market input, features, regime, model output, signal, risk decision and expected final action, and CI fails on any unexplained change.

- **If violated:** A refactor changes what the bot trades and nobody notices until the PnL does.
- **Owned by:** `src/engine/signal_engine.py`, `scripts/generate_signal_fixtures.py`
- **Verification:**
  - `tests/signals/test_golden_signals.py` (signal) — Six cases covering clean long, clean short, flat signal, regime halt, corrupt data and stale data. Also asserts the fixture set has not become trivial, which a round-trip check alone cannot see.

> The model's p_long is supplied by the case rather than computed: a retrained tree on a different library build produces different probabilities, so pinning it would fail for reasons unrelated to this project. Everything downstream of it is real production code.

#### `SIG-002` — Feature computation is deterministic and reproducible

**VERIFIED** · high · requirement · source: QE-86

The same input bars produce bit-identical features across runs and processes.

- **If violated:** A backtest cannot be reproduced, so a result cannot be trusted.
- **Owned by:** `src/features/pipeline.py`
- **Verification:**
  - `tests/features/test_feature_determinism.py` (property) — Same process, different process (two subprocesses with deliberately different PYTHONHASHSEED), and prefix stability when later bars arrive.
  - `tests/test_features.py` (unit)

#### `SIG-003` — Mutation score on the signal subsystem is at or above 85%

**VERIFIED** · medium · requirement · source: QE-49

Nightly mutation testing of the signal modules kills at least 85% of generated mutants.

- **If violated:** Signal tests assert that something was produced, not that it was right.
- **Owned by:** `config/mutation_thresholds.json`, `.github/workflows`
- **Verification:**
  - `tests/regression/test_regression_registry_contract.py` (mutation) — The signal subsystem's 85% floor.

#### `SIG-004` — The eighteen-engine seam is asserted from both sides, positions included

**VERIFIED** · high · requirement · source: OPS-2026-09-21

Every eNN_*.py module declares an _ENGINE_ID matching its filename and exposes one engine class with `async run(self, symbol, data)`; every engine given empty data abstains into a contract-valid EngineOutput rather than raising; EngineOrchestrator registers exactly eighteen engines in the order its positional attribution assumes; every engine has an SLA and every SLA an engine; and a cycle accounts for all eighteen as either an output or a named failure, each output honouring the same contract the producers were held to.

- **If violated:** run() attributes result i to E-{i+1} by position, so reordering the registration list files every output under the wrong engine, applies every SLA to the wrong engine and names the wrong thing in every log line -- silently, with no exception and no failing per-engine test.
- **Owned by:** `src/engines/orchestrator.py`, `src/engines/schema.py`
- **Verification:**
  - `tests/test_engine_seam_contract.py` (integration) — Asserts the contract from the producer side per engine and again where the consumer receives it, pins the list order against the positional attribution in run(), and runs one real cycle to prove no engine is dropped or duplicated. Swapping two entries in the registration list fails only this test -- every other test in the suite still passes, which is why it exists.

#### `REG-0008` — On-chain features never report a fabricated constant

**VERIFIED** · high · regression · source: OPS-2026-09-21

SOPR, NVT and MVRV are either genuinely computed or NaN; no on-chain metric returns a finite value that varies with none of its inputs, and an RPC outage is reported as NaN rather than as a neutral-looking reading.

- **If violated:** A constant 1.0 SOPR reached the live feature dict in src/intel.py and was persisted to intelligence_features_history beside measured columns, so models trained and traded on a fabricated on-chain signal, and a bitcoind outage was indistinguishable from a neutral market.
- **Owned by:** `src/features/onchain.py`
- **Verification:**
  - `tests/test_onchain_no_fabricated_values.py` (regression)

> Escaped to production: the constant reached the live feature dict in src/intel.py and was persisted to intelligence_features_history beside measured columns. The test suite could not catch it because the test that existed asserted the placeholder (== 1.0) rather than the requirement, so the layer that should have caught it is the review that accepted a return of a bare literal from a metric together with a docstring describing a computation the body never performed. layer: review

## Models and leakage

#### `MODL-001` — Every model artifact carries full provenance

**VERIFIED** · high · requirement · source: QE-44

A model artifact records model id, training-data hash, feature-schema hash, code commit, hyperparameters, seed, library versions, metrics, validation methodology and artifact hash.

- **If violated:** A model in production cannot be traced to the data or code that made it.
- **Owned by:** `src/models/provenance.py`, `src/models/trainer.py`
- **Verification:**
  - `tests/models/test_model_provenance.py` (model) — All ten fields, with each hash asserted to change when its subject changes and not to change when something irrelevant does.
  - `tests/models/test_model_artifacts.py` (model) — The manifest written by save() carries a complete record, and the legacy {file, sha256} keys still verify.
  - `tests/test_model_registry.py` (model)

#### `MODL-002` — A model never decides to bypass the risk gate

**VERIFIED** · critical · requirement · source: QE-45

Model output reaches an order only through SIGNAL -> RISK ENGINE -> EXECUTION POLICY; no module lets a model result skip the deterministic safety layer.

- **If violated:** A confident model overrides the control designed to survive a confident model being wrong.
- **Owned by:** `src/engine/signal_engine.py`, `src/risk/gates.py`
- **Verification:**
  - `tests/models/test_model_safety.py` (security) — Behavioural (confidence is not an input to any gate) and structural (no model or intelligence module imports an executor, and the engine does not discard the gate verdict).

#### `MODL-003` — No lookahead: a signal at T is invariant to data after T

**VERIFIED** · critical · requirement · source: QE-47

For a dataset truncated at T and the same dataset extended with arbitrary future data, the signal computed at T is identical.

- **If violated:** The backtest is a fantasy and the live strategy loses money the simulation never showed.
- **Owned by:** `src/models/leakage.py`, `src/features/pipeline.py`
- **Verification:**
  - `tests/models/test_lookahead.py` (verification) — The detector is first shown to catch four deliberate leaks and to clear an honest feature, then run against the real pipeline with both real and absurd future data.

#### `MODL-004` — Model drift beyond threshold demotes the model

**VERIFIED** · high · requirement · source: QE-46

Feature, prediction, confidence, regime, calibration, performance and missingness drift are monitored; breaching a threshold moves the model to degraded/shadow rather than continuing to trust it.

- **If violated:** The model keeps trading a regime it was never trained on.
- **Owned by:** `src/risk/performance_drift.py`, `src/risk/drift_integration.py`
- **Verification:**
  - `tests/models/test_model_drift_demotion.py` (component) — A healthy model is not demoted, too little evidence is not drift, a degraded model halts the live track only, and a detector that cannot run fails closed.
  - `tests/test_performance_drift.py` (component)
  - `tests/test_drift_gate_wiring.py` (integration)

#### `MODL-005` — Model artifacts round-trip and reproduce

**VERIFIED** · high · requirement · source: QE-44

Each registered model can be loaded, predicted from, serialised, deserialised and reproduced to the recorded metrics from the recorded seed.

- **If violated:** A model that cannot be reloaded cannot be rolled back to.
- **Owned by:** `src/models/trainer.py`
- **Verification:**
  - `tests/models/test_model_artifacts.py` (model) — Load, predict, serialise, deserialise and reproduce bit-for-bit; a tampered artifact, a missing manifest and a swapped manifest are all refused.

#### `MODL-006` — Research output cannot change production parameters directly

**VERIFIED** · critical · requirement · source: QE-83

A notebook or research script has no path to production trading parameters; promotion runs RESEARCH -> candidate -> validation -> shadow -> paper -> production.

- **If violated:** An experiment becomes the production strategy by accident.
- **Owned by:** `src/upgrade/shadow_deploy.py`, `src/tuning/promotion_gauntlet.py`
- **Verification:**
  - `tests/models/test_research_firewall.py` (verification) — The gauntlet is a conjunction with no partial credit, shadow evaluation is a time window a challenger cannot buy its way past, and no tuning or upgrade module imports an executor.
  - `tests/test_shadow_model_promotion.py` (verification)
  - `tests/test_upgrade_shadow_deploy.py` (verification)

#### `MODL-007` — Cross-validation respects time and purging

**VERIFIED** · high · requirement · source: QE-90,QE-86

Model validation uses combinatorial purged cross-validation with an embargo, so adjacent-sample leakage cannot inflate the reported score.

- **If violated:** A leaky validation split reports skill the strategy does not have.
- **Owned by:** `src/models/trainer.py`
- **Verification:**
  - `tests/models/test_cpcv_purging.py` (verification) — Train and test never overlap, the purge gap before and the embargo after every test block are empty, and the fold count is the binomial coefficient rather than a single pass.

## Data, money and time

#### `INV-008` — A stale market-data sample cannot be treated as current

**VERIFIED** · critical · invariant · source: QE-42,QE-80

A sample older than the declared freshness budget is not used for a trading decision unless an explicit, named policy permits it.

- **If violated:** The bot trades a price that no longer exists.
- **Owned by:** `src/data/quality_gate.py`, `src/engine/signal_engine.py`
- **Depends on:** `DATA-002`
- **Verification:**
  - `tests/component/test_data_quality_gate.py` (component) — Staleness measured against a declared budget, with the budget named in the rejection.
  - `tests/signals/test_golden_signals.py` (signal) — Golden case ETHUSDT_15m_case_003: a well-formed frame that stopped updating an hour ago is refused, and no risk decision is reached.
  - `tests/engines/test_supply_and_quality.py` (component) — Exercises the quality gate.

#### `DATA-001` — Market data failing the quality gate never reaches the signal engine

**VERIFIED** · critical · requirement · source: QE-80

Schema, timestamp validity, monotonicity, freshness, OHLC consistency, non-negative volume, and absence of NaN/Infinity are all checked before use; failure means NO TRADE.

- **If violated:** A corrupt bar becomes a signal, and the signal becomes an order.
- **Owned by:** `src/data/quality_gate.py`, `src/engine/signal_engine.py`
- **Verification:**
  - `tests/component/test_data_quality_gate.py` (component) — Every line of the source document's checklist, asserted on the rejection reason as well as on pass/fail.
  - `tests/signals/test_golden_signals.py` (signal) — Golden case ETHUSDT_15m_case_002: one impossible bar refuses the frame before the feature pipeline is reached.
  - `tests/engines/test_supply_and_quality.py` (component)
  - `tests/test_feature_bar_gaps.py` (unit)

> The gate existed and was tested from the day it was written, but no module in src/ ever called it -- so the statement above was false of the running system until PR-003 wired it into SignalEngine.tick.

#### `DATA-002` — Data freshness budgets are declared and enforced

**VERIFIED** · critical · requirement · source: QE-80,QE-42

Every market-data consumer declares a maximum age, and a sample past it is refused rather than used.

- **If violated:** A frozen feed looks like a calm market.
- **Owned by:** `src/data/quality_gate.py`
- **Verification:**
  - `tests/component/test_data_quality_gate.py` (component) — FreshnessBudget.for_timeframe derives the budget from the bar interval; the rejection names the budget that refused it.

#### `DATA-003` — A single clock policy: internal timestamps are UTC and exchange skew is bounded

**VERIFIED** · high · requirement · source: QE-79

All internal timestamps are timezone-aware UTC; exchange-versus-local skew beyond the declared bound is detected and reported.

- **If violated:** A DST shift or a naive datetime silently moves a bar an hour.
- **Owned by:** `src/data/clock.py`
- **Verification:**
  - `tests/component/test_clock_policy.py` (component) — Naive datetimes refused rather than assumed, other zones converted rather than relabelled, the DST gap and repeated hour shown to be absent in UTC, and venue skew measured against a declared budget.

#### `DATA-004` — Money arithmetic uses a declared exact representation

**VERIFIED** · high · requirement · source: QE-78

Where money is added, compared or rounded, the representation is documented and exact; float is used only where the doc says it may be.

- **If violated:** Rounding drift accumulates until the ledger and the exchange disagree.
- **Owned by:** `src/execution/unified_ledger.py`, `src/risk/kelly.py`, `docs/quality`
- **Verification:**
  - `tests/component/test_money_representation.py` (component) — The declared policy is float64 with Decimal at the exchange-precision boundary; quantisation never rounds up, accumulated drift over a day of fills stays inside 1e-9 relative, and the percent-versus-fraction units are pinned.
  - `tests/test_percent_vs_fraction_units.py` (unit)
  - `tests/test_unified_ledger.py` (unit)

> The representation is float64, not Decimal, and docs/quality/MONEY_AND_TIME.md argues for that rather than claiming otherwise. It also records the conditions under which the decision should be re-argued.

#### `DATA-005` — Tick size, lot size and minimum quantity are respected

**VERIFIED** · high · requirement · source: QE-78

Every submitted order conforms to the venue's tick, lot and minimum-notional rules, checked before submission.

- **If violated:** Orders are rejected by the venue at exactly the moment the strategy needs them.
- **Owned by:** `src/risk/kelly.py`, `src/data/fetcher.py`
- **Verification:**
  - `tests/execution/test_venue_precision.py` (unit) — Quantisation never increases a quantity, and every minimum is compared against the quantised value -- the number the venue actually sees.
  - `tests/test_fetcher_symbol_precision.py` (unit)

#### `REG-0009` — Each pytest process owns its own DuckDB file

**VERIFIED** · medium · regression · source: OPS-2026-09-21

Every pytest process, controller or xdist worker, resolves DUCKDB_PATH to a file only it opens; no two processes in a run share a DuckDB database.

- **If violated:** Workers inherit the controller's DUCKDB_PATH, DuckDB takes an exclusive lock, and whichever worker opens the file second dies with 'Conflicting lock is held', failing a shard at random and reading as a flake.
- **Owned by:** `tests/conftest.py`
- **Verification:**
  - `tests/test_duckdb_path_is_per_worker.py` (regression)

> Escaped to the test suite, not to a running system: production reads DUCKDB_PATH from its own environment and never had two processes sharing one file. What broke was the suite's own isolation, and only under -n, so it presented as a flake that moved between shards rather than as a defect. Filed because a red shard nobody can attribute is a false signal in the gate that merges every change. layer: test-suite

## API and WebSocket

#### `API-001` — The authorization matrix is executable and every cell is tested

**VERIFIED** · critical · requirement · source: QE-19

For each (role, endpoint) pair the declared allow/deny outcome is asserted by a test, including the anonymous row.

- **If violated:** A readonly token places a trade.
- **Owned by:** `src/api/access_control.py`, `src/api/main.py`
- **Verification:**
  - `tests/test_api_role_enforcement.py` (api)
  - `tests/test_access_control.py` (security)
  - `tests/authorization/test_authorization_matrix.py` (security) — Every (role, endpoint) cell asserted, including the anonymous row and the escalation direction.

#### `API-002` — No insecure direct object references

**VERIFIED** · critical · requirement · source: QE-20

A caller cannot read or modify another principal's resource by substituting an identifier.

- **If violated:** One authenticated user reads or cancels another's orders.
- **Owned by:** `src/api/access_control.py`, `src/api/object_refs.py`
- **Verification:**
  - `tests/authorization/test_object_references.py` (security) — Identifiers are validated before any lookup, and every negative outcome returns one indistinguishable body.

#### `API-003` — Injection payloads are rejected at the boundary

**VERIFIED** · critical · requirement · source: QE-21

SQL, NoSQL, command, template, path-traversal, header and JSON-manipulation payloads are refused wherever input reaches a database, filesystem, subprocess, external API or log.

- **If violated:** An attacker reads or rewrites the trading database.
- **Owned by:** `src/api/main.py`, `src/api/middleware.py`
- **Verification:**
  - `tests/api/test_injection_and_rate_limiting.py` (security) — Injection payloads refused at the parsers, the outbound surface and the header boundary.

#### `API-004` — Outbound URL handling is SSRF-resistant

**VERIFIED** · critical · requirement · source: QE-22

Any caller-influenced outbound request refuses localhost, loopback, link-local, private ranges, cloud metadata endpoints and internal hostnames.

- **If violated:** The bot becomes the attacker's proxy into the private network and the metadata service.
- **Owned by:** `src/intelligence/client.py`, `src/api/ssrf.py`
- **Verification:**
  - `tests/api/test_ssrf_protection.py` (security) — Every resolved address is checked, the metadata service and private ranges are denied, and the guard is wired into the outbound client.

#### `API-005` — WebSocket payloads are validated regardless of connection state

**VERIFIED** · critical · requirement · source: QE-23

An established connection confers no trust: every message is authenticated, schema-validated, size-bounded, rate-limited and replay-checked.

- **If violated:** A single authenticated socket becomes an unauthenticated command channel.
- **Owned by:** `src/api/main.py`, `src/data/orderbook_stream.py`, `src/api/ws_guard.py`
- **Verification:**
  - `tests/test_ws_auth_query_param.py` (security)
  - `tests/api/test_websocket_frame_guard.py` (security) — Every inbound frame is size-bounded, schema-validated, freshness-checked, rate-limited and replay-checked, in that order, with one guard per connection.
  - `tests/authentication/test_api_authentication.py` (security) — The upgrade itself is authenticated before any state is touched.

#### `API-006` — Rate limiting protects authentication, trading and expensive endpoints

**VERIFIED** · high · requirement · source: QE-24

Login, authentication, trade endpoints, sensitive mutations, WebSocket connections and expensive queries are rate-limited, and the limits are tested under burst and sustained load.

- **If violated:** An attacker with a valid token issues thousands of orders before anything notices.
- **Owned by:** `src/api/middleware.py`
- **Verification:**
  - `tests/test_selftest_rate_limit.py` (api)
  - `tests/api/test_injection_and_rate_limiting.py` (api) — Rate limiting on the authentication, trading and diagnostic endpoints, keyed per client rather than globally.

#### `API-007` — Security headers are present and correct

**VERIFIED** · medium · requirement · source: QE-18

Responses carry the declared security headers, and a test fails if one is removed.

- **If violated:** A browser-side weakness that the headers would have closed.
- **Owned by:** `src/api/middleware.py`, `src/api/security_headers.py`
- **Verification:**
  - `tests/api/test_security_headers.py` (security) — Each declared header is applied, HSTS only over TLS, and the middleware sits outermost so an error response still carries them.

#### `API-008` — A failing security control never opens a trading endpoint

**VERIFIED** · critical · requirement · source: QE-55

If authentication, authorization, rate limiting or logging is unavailable, the trading endpoints refuse rather than degrade to open.

- **If violated:** An outage in the auth dependency turns the trade endpoint anonymous.
- **Owned by:** `src/api/auth.py`, `src/api/middleware.py`, `src/api/fail_closed.py`
- **Depends on:** `GOV-004`
- **Verification:**
  - `tests/api/test_fail_closed_controls.py` (security) — A control that never reported counts as degraded, any single degradation closes the trading endpoints, and the read-only endpoints stay open by design.

#### `API-009` — Error responses leak neither secrets nor internals

**VERIFIED** · high · requirement · source: QE-18,QE-17

No error path returns a credential, token, stack trace or internal hostname to a caller.

- **If violated:** A 500 hands the attacker the next step.
- **Owned by:** `src/api/main.py`, `src/api/error_hygiene.py`
- **Depends on:** `SECR-001`
- **Verification:**
  - `tests/api/test_error_hygiene.py` (security) — Tracebacks, connection URIs, internal hosts and echoed validation input are all replaced, while the endpoints' own messages survive.

## Cryptography and secrets

#### `SECR-001` — Secrets never appear in source, images, logs or workflow YAML

**VERIFIED** · critical · requirement · source: QE-9,QE-17

Deliberately triggering errors that involve an API key, secret, JWT, database password, authorization header or exchange credential produces output containing none of them.

- **If violated:** The exchange key is in a log file that gets shipped to a third-party log service.
- **Owned by:** `src/logging_setup.py`, `common/command_schema.py`
- **Verification:**
  - `tests/test_logging_setup.py` (security)
  - `tests/test_command_schema_policy.py` (security)
  - `tests/security/test_secret_leak_provocation.py` (security) — Provokes the leak rather than asserting its absence: keys, connection URIs, JWTs and private keys are pushed through the log processor and through rendered exceptions.

#### `SECR-002` — Secret comparisons are constant-time

**VERIFIED** · high · requirement · source: QE-12

Every comparison of a token, signature, key or password uses a constant-time primitive, and a test detects a reversion to `==`.

- **If violated:** A timing oracle recovers a token one byte at a time.
- **Owned by:** `src/security/constant_time.py`
- **Verification:**
  - `tests/test_static_invariants.py` (security)
  - `tests/security/test_signing_and_constant_time.py` (security) — The helpers delegate to hmac.compare_digest, and a source-tree scan asserts no secret-named value is compared with == anywhere in src/.

#### `SECR-003` — Keys are environment-separated and rotatable, and rotation is tested

**VERIFIED** · critical · requirement · source: QE-11

Development, test, paper and production keys are distinct; rotation for normal, compromise, departure, server-compromise, exchange-incident and GitHub-compromise cases is documented and exercised.

- **If violated:** A procedure nobody has run fails on the day it is needed.
- **Owned by:** `src/security/credential_vault.py`, `src/security/key_lifecycle.py`
- **Verification:**
  - `tests/security/test_key_lifecycle.py` (security) — One master seed still yields distinct material per environment and per exchange; rotation advances generations, and compromise and departure revoke the previous one immediately.

#### `SECR-004` — The audit log is a verifiable hash chain

**VERIFIED** · critical · requirement · source: QE-16

Each event hashes its own content plus the previous event's hash; modifying, deleting or reordering any event fails verification.

- **If violated:** An attacker who reaches the host edits the record of what they did.
- **Owned by:** `src/diagnostics/audit_trail.py`
- **Verification:**
  - `tests/test_audit_trail.py` (security)
  - `tests/test_audit_chain_integrity_endpoint.py` (api)
  - `tests/security/test_audit_chain_tamper_evidence.py` (security) — Each tampering is performed and then detected: edited field, recomputed hash, deletion, reordering, splice. Tip truncation and the eviction window are stated as the limits they are.

#### `SECR-005` — Signed requests reject tampering and replay

**VERIFIED** · critical · requirement · source: QE-12

A correct signature is accepted; a modified request, timestamp or parameter is rejected; a replayed request is rejected where the protocol allows it.

- **If violated:** A captured order request is replayed to double a position.
- **Owned by:** `src/security/api_signer.py`
- **Verification:**
  - `tests/engines/test_security.py` (security)
  - `tests/security/test_signing_and_constant_time.py` (security) — Tampering with any signed field breaks verification; stale, post-dated and replayed requests are refused by ReplayWindow, and an unverifiable signature never reaches its cache.

#### `SECR-006` — TLS is verified everywhere and never disabled to make a test pass

**VERIFIED** · critical · requirement · source: QE-14,QE-15

HTTPS, secure WebSocket and TLS database connections are required; expired certificates, hostname mismatch, weak protocols, broken chains and plaintext endpoints are detected; no code path disables verification.

- **If violated:** An on-path attacker rewrites market data or order responses.
- **Owned by:** `src/intelligence/client.py`, `src/security/tls.py`
- **Verification:**
  - `tests/security/test_randomness_and_tls.py` (security) — Cleartext destinations are refused except loopback, and a scanner asserts no source file spells verify=False, ssl=False, CERT_NONE, tlsInsecure or their relatives.

#### `SECR-007` — Cryptographic randomness comes from the OS CSPRNG

**VERIFIED** · critical · requirement · source: QE-12

Nonces, keys, tokens and identifiers that must be unpredictable come from `secrets`/`os.urandom`, never from `random`.

- **If violated:** A predictable token is guessable.
- **Owned by:** `src/security/credential_vault.py`, `src/security/randomness.py`
- **Verification:**
  - `tests/security/test_randomness_and_tls.py` (security) — Every unpredictable value comes from secrets, a short request is refused rather than quietly served, and src/security never imports random.

#### `SECR-008` — No custom cryptographic primitives

**VERIFIED** · critical · requirement · source: QE-8

The application calls established libraries; it does not implement its own cipher, hash construction, KDF or signature scheme for security purposes.

- **If violated:** A homemade primitive fails in a way only an attacker notices.
- **Owned by:** `src/security/pq_transport.py`
- **Verification:**
  - `tests/test_security_pqc_posture.py` (security)
  - `tests/security/test_no_custom_primitives.py` (security) — Each security module delegates to cryptography, hmac, hashlib or secrets; no hand-rolled cipher, key schedule or comparison loop, and mathcore is kept out of the security path.

#### `SECR-009` — Sensitive data at rest is encrypted, and the key is managed separately

**VERIFIED** · high · requirement · source: QE-13

Database backups, production databases, sensitive model artifacts, audit archives and configuration backups are encrypted, with keys held outside the encrypted store.

- **If violated:** Ciphertext and key are stolen together and the encryption bought nothing.
- **Owned by:** `src/security/credential_vault.py`, `src/security/at_rest.py`
- **Verification:**
  - `tests/security/test_at_rest_encryption.py` (security) — AES-256-GCM round trip, and the restore path under every failure that has caused a real outage: wrong key, truncation, flipped byte, wrong version, mismatched associated data -- all one exception type.

#### `SECR-010` — The exchange key the bot holds cannot withdraw

**VERIFIED** · critical · requirement · source: QE-10,QE-67

The documented and verified key posture is trading-only, withdrawal-disabled, IP-restricted where supported, with separate paper and production keys on separate subaccounts.

- **If violated:** A compromised host becomes a withdrawal, not just a bad trade.
- **Owned by:** `src/security/credential_vault.py`, `src/security/exchange_key_posture.py`
- **Verification:**
  - `tests/security/test_exchange_key_posture.py` (security) — A withdrawal-capable or undeclared posture is refused, the shipped declaration is checked, and LiveExecutor asserts it before building any state. The live-venue verification remains a documented human step.

#### `SECR-011` — Deposit-address derivation cannot produce or accept a private key

**VERIFIED** · critical · requirement · source: OPS-2026-09-22

src/mathcore/derivation/bip32.py derives extended public keys only. It refuses hardened indices, refuses an extended private key by name, and exposes no function or dataclass field that could hold private key material -- a property asserted structurally by inspecting every public signature, not only behaviourally.

- **If violated:** Spending authority present in the process that generates deposit addresses. A trading host is internet-facing and runs untrusted market data through itself; an attacker who reads its memory should learn which addresses to watch and nothing that moves funds. The specific BIP-32 trap is that an extended public key plus any one non-hardened child private key yields the parent private key by subtraction, and therefore the whole branch -- so a module that held both at once would hand over the tree.
- **Owned by:** `src/mathcore/derivation/bip32.py`
- **Verification:**
  - `tests/test_bip32.py` (security)

#### `SECR-012` — Derived addresses match what a BIP-32 wallet derives, checked against an independent oracle

**VERIFIED** · high · requirement · source: OPS-2026-09-22

Watch-only public derivation agrees with private derivation from the published seed for every non-hardened path tested, and each published master xpub the tests rely on is verified to be what its published seed actually derives rather than trusted as transcribed. Base58Check is validated on parse, so a mistyped extended key is refused instead of decoding to a different valid-looking key.

- **If violated:** Deposit addresses that no sender ever pays. A derivation that is self-consistent but disagrees with the standard -- an uncompressed point in the HMAC input, the chain code taken from the wrong half of the digest, a missing reduction mod n -- produces a tree that looks correct in isolation and matches no other wallet. Verifying against a transcribed expected string does not catch it either: one such string was misremembered while writing these tests, which is how the checksum requirement was found.
- **Owned by:** `src/mathcore/derivation/bip32.py`
- **Depends on:** `SECR-011`
- **Verification:**
  - `tests/test_bip32.py` (verification)

#### `SECR-013` — The threshold module supplies sharing primitives and refuses to be a signer

**VERIFIED** · critical · requirement · source: OPS-2026-09-22

src/mathcore/derivation/threshold.py exposes Feldman commitments, group and participant public keys, share verification, Lagrange weights at zero and dispute evidence, and exposes no function for nonce generation, nonce aggregation, challenge computation or partial-signature combination. The absence is asserted by inspecting every public function name, and the module holds no random source -- the caller supplies the polynomial coefficients.

- **If violated:** An ad-hoc threshold Schnorr scheme assembled in-house from correct parts. The Wagner ROS attack forges a signature across many concurrently-opened signing sessions, so the aggregation is broken even though every primitive it is built from verifies in isolation -- which is the most dangerous shape a crypto library can have, because nothing fails a test. The defence is a scheme with a proof of concurrent security (FROST, RFC 9591, or MuSig2), so the pieces are deliberately left unassembled and a test fails if a signing-shaped function appears.
- **Owned by:** `src/mathcore/derivation/threshold.py`
- **Verification:**
  - `tests/test_threshold.py` (security)

#### `SECR-014` — A tampered share is rejected and too few shares are refused, not approximated

**VERIFIED** · high · requirement · source: OPS-2026-09-22

verify_share checks a share against the dealer's commitments and rejects any value that is not the committed polynomial evaluated at that participant's identifier, including a share valid at a different identifier or under a different dealer. Reconstruction refuses fewer shares than the threshold and refuses duplicate identifiers rather than interpolating an under-determined polynomial.

- **If violated:** A custody key that cannot be reconstructed at the moment funds must move, or one reconstructed to the wrong value. A tampered or mis-delivered share surfaces only at reconstruction unless it is verified on receipt; and interpolating fewer than the threshold number of points returns a number indistinguishable from the real secret, so a caller who passes three shares from two distinct holders would sign with a key nobody controls and nothing in the arithmetic could notice.
- **Owned by:** `src/mathcore/derivation/threshold.py`
- **Depends on:** `SECR-013`
- **Verification:**
  - `tests/test_threshold.py` (verification)

#### `SECR-015` — A received Diffie-Hellman public value is confined to the prime-order subgroup

**VERIFIED** · critical · requirement · source: OPS-2026-09-22

is_valid_public_value accepts exactly the quadratic residues other than 1 for a safe prime p, rejecting 0, 1, p-1, any value at or above p-1, and every full-order element. Completeness is asserted by enumerating the subgroup by brute force for small safe primes and comparing set equality, not by asserting the implementation against itself.

- **If violated:** Small-subgroup confinement. A peer sends a public value of order 1 or 2 and the shared secret takes one of one or two values, which the attacker simply tries. Validating the parameters does not prevent this -- it proves which subgroups exist, not what arrived on the wire -- and a deployment with a flawless RFC 7919 modulus and no value check is exactly the combination Valenta et al. measured in the field.
- **Owned by:** `src/mathcore/numbertheory/safe_primes.py`
- **Verification:**
  - `tests/test_safe_primes.py` (security)

#### `SECR-016` — A prime modulus whose (p-1)/2 is composite is reported as unsafe, and no named group constant is hardcoded

**VERIFIED** · high · requirement · source: OPS-2026-09-22

is_safe_prime requires both p and (p-1)/2 prime, and validate_group reports every problem it finds rather than the first. The module ships no RFC 7919 modulus: the caller supplies one and its structure is verified. order_of verifies p is a safe prime before relying on the orders being confined to 1, 2, q and 2q.

- **If violated:** Parameters that pass a naive 'is p prime' check while p-1 remains smooth, which is the precondition a confinement attack needs -- and a caller with no diagnostic distinguishing a composite modulus from a prime that is merely not safe, which have different fixes. Separately, order_of shipped a real instance of the silent-wrong-answer class: it documented a safe-prime assumption without checking it, so for p=13 and y=3 it returned 6, a multiple of the true order 3, shaped exactly like the order.
- **Owned by:** `src/mathcore/numbertheory/safe_primes.py`
- **Depends on:** `SECR-015`
- **Verification:**
  - `tests/test_safe_primes.py` (validation)

## Supply chain and artifacts

#### `SUP-001` — Every workflow declares least-privilege permissions

**VERIFIED** · critical · requirement · source: QE-28

Each workflow sets `permissions:` explicitly at the top level, starting from `contents: read`, and no workflow uses write-all.

- **If violated:** A compromised action inherits write access to the repository.
- **Owned by:** `.github/workflows`, `scripts/check_supply_chain.py`
- **Verification:**
  - `tests/supply_chain/test_supply_chain_posture.py` (security) — Every workflow declares explicit permissions, blanket grants are refused, and an unexplained top-level write scope fails -- checked against this repository and against synthetic violations.

#### `SUP-002` — Third-party actions are pinned to a full commit SHA

**VERIFIED** · critical · requirement · source: QE-28

No workflow references an action by tag or branch; every `uses:` names a 40-character SHA.

- **If violated:** A retagged action runs attacker code inside CI with the repository's secrets.
- **Owned by:** `.github/workflows`, `scripts/check_supply_chain.py`
- **Verification:**
  - `tests/supply_chain/test_supply_chain_posture.py` (security) — Every `uses:` names a 40-hex SHA with a version comment; tags, branches, short SHAs and uncommented pins are all detected.

#### `SUP-003` — Fork pull requests cannot reach production secrets

**VERIFIED** · critical · requirement · source: QE-29,QE-30

No workflow triggered by a fork pull request has access to a production secret, and this is asserted rather than assumed.

- **If violated:** A malicious PR prints the exchange key by editing a test.
- **Owned by:** `.github/workflows`, `scripts/check_supply_chain.py`
- **Verification:**
  - `tests/supply_chain/test_supply_chain_posture.py` (security) — pull_request_target is refused outright, and any stored secret reachable from a pull request must carry a fork guard; GITHUB_TOKEN is excluded because permissions bound it.

#### `SUP-004` — Release artifacts carry version, commit, lock, SBOM, hash and attestation

**VERIFIED** · high · requirement · source: QE-37

Every deployable artifact is accompanied by its provenance, and production verifies the artifact is exactly the one CI produced.

- **If violated:** Nobody can prove what is running in production.
- **Owned by:** `.github/workflows`, `scripts/write_provenance.py`, `.github/workflows/release.yml`
- **Verification:**
  - `tests/supply_chain/test_artifact_provenance.py` (security) — The record is verified from the uploaded artifact, and every corruption is caught: modified bytes, missing file, undescribed extra, wrong commit, emptied field, wrong schema version.

#### `SUP-005` — Dependency vulnerabilities are surveilled continuously

**VERIFIED** · high · requirement · source: QE-27,QE-72

Daily alerts, weekly dependency review, monthly full audit and an immediate path for a critical CVE are all configured and exercised.

- **If violated:** A known-exploited dependency sits in production for weeks.
- **Owned by:** `.github/workflows`, `.github/dependabot.yml`
- **Verification:**
  - `tests/supply_chain/test_supply_chain_posture.py` (security) — Dependabot covers pip, npm, github-actions and docker on a schedule, and the check fails when an ecosystem this repository has is missing.

#### `SUP-006` — Container images run non-root with minimal capability

**VERIFIED** · high · requirement · source: QE-36

Images use a non-root user, a minimal base, a read-only filesystem where possible, dropped capabilities, no privileged mode, no host networking unless required, and resource limits -- and CI tests those properties.

- **If violated:** A container escape becomes host root.
- **Owned by:** `.github/workflows`, `Dockerfile`
- **Verification:**
  - `tests/supply_chain/test_container_posture.py` (security) — Non-root numeric user, root-owned code, multi-stage so no toolchain ships, digest-pinned slim base; CI builds the image, proves /app is unwritable under --read-only --cap-drop=ALL, and Trivy fails the job on HIGH/CRITICAL.

#### `SUP-007` — A dependency update cannot become a production deployment on its own

**VERIFIED** · critical · requirement · source: QE-27

Automated dependency PRs run the full quality gate and can never trigger the production workflow without the same approvals as any other change.

- **If violated:** A compromised package auto-deploys itself.
- **Owned by:** `.github/workflows`, `scripts/check_supply_chain.py`
- **Verification:**
  - `tests/supply_chain/test_supply_chain_posture.py` (security) — The release workflow triggers only on a version tag and workflow_dispatch; push-on-branch, pull_request and schedule are all detected, including a tag trigger that also accepts branches.

## Resilience and recovery

#### `RES-001` — Every component failure has a declared, tested fail-safe behaviour

**VERIFIED** · critical · requirement · source: QE-26

Price feed, risk engine, exchange status, analytics, model, audit, database and WebSocket each have a documented failure policy, and a test asserts the system takes it.

- **If violated:** Fail-open is chosen by default because no one chose anything.
- **Owned by:** `src/diagnostics/runtime_monitor.py`, `src/diagnostics/failsafe_policy.py`
- **Verification:**
  - `tests/recovery/test_failsafe_policy.py` (recovery) — Every component has a declared response, every degradation names what is lost and expires into a halt, and an undeclared component resolves to HALT_ALL rather than to carrying on.

#### `RES-002` — Crash during a fill replays to a safe, reconciled state

**VERIFIED** · critical · requirement · source: QE-53

Killing the process at each stage of an order lifecycle leaves a state the recovery path can reconcile without duplicating or losing an order.

- **If violated:** A restart loses or doubles a live position.
- **Owned by:** `src/diagnostics/disaster_recovery.py`
- **Verification:**
  - `tests/recovery/test_crash_replay.py` (recovery) — The process is killed at each stage of the order lifecycle; the dangerous gap (venue filled, no local record) is reported rather than averaged away, and the replay cannot double the order.

#### `RES-003` — Backups are encrypted, off-host and restore-tested

**VERIFIED** · high · requirement · source: QE-39

A scheduled drill restores from backup and verifies the result; a backup that has never been restored is not counted.

- **If violated:** The backup turns out to be unreadable on the day it is needed.
- **Owned by:** `src/diagnostics/disaster_recovery.py`, `src/diagnostics/recovery_objectives.py`
- **Verification:**
  - `tests/test_disaster_recovery.py` (recovery)
  - `tests/recovery/test_recovery_objectives.py` (recovery) — The drill measures rather than asserts: slow, stale and throwing restores each fail, and a restore that raises is a failed drill instead of a crashed drill run.

#### `RES-004` — RTO and RPO are declared and measured

**VERIFIED** · medium · requirement · source: QE-41

Recovery time and data-loss objectives carry actual numbers chosen for this operation, and a drill measures whether they are met.

- **If violated:** Recovery takes longer than anyone assumed and nobody had a number to check against.
- **Owned by:** `src/diagnostics/disaster_recovery.py`, `src/diagnostics/recovery_objectives.py`
- **Verification:**
  - `tests/recovery/test_recovery_objectives.py` (recovery) — Every data class carries a number and a justification; trade records tolerate no loss while market history tolerates a minute, and RTO is tighter with open positions than flat.

#### `RES-005` — Concurrency produces no double order, lost order or corrupted position

**VERIFIED** · critical · requirement · source: QE-52

Simultaneous signals on one symbol, and an order request interleaved with disconnect, reconnect and a duplicate response, leave exactly one consistent outcome.

- **If violated:** A race doubles a position under exactly the market conditions that caused the race.
- **Owned by:** `src/execution/order_manager.py`, `src/execution/idempotency.py`
- **Verification:**
  - `tests/test_runtime_monitor_concurrent_probes.py` (resilience)
  - `tests/recovery/test_concurrency_races.py` (recovery) — Fifty coroutines race one idempotency key and exactly one wins; concurrent completion and failure cannot release a claimed key, and distinct decisions never collide into one key.

#### `RES-006` — Performance baselines exist and regressions are flagged

**VERIFIED** · medium · requirement · source: QE-54

p50/p95/p99, error rate, CPU and memory baselines are recorded for feature generation, signal generation, inference, risk evaluation, order processing, database, API and WebSocket, and a large regression fails or escalates.

- **If violated:** The bot becomes too slow to act on its own signals and nothing says so.
- **Owned by:** `src/diagnostics/instrumentation.py`, `src/diagnostics/performance_baseline.py`, `config/performance_baselines.json`
- **Verification:**
  - `tests/performance/test_performance_baselines.py` (performance) — Percentile budgets for the whole trading path, with a tail regression caught where a mean would hide it, and an undeclared operation raising rather than passing silently.

#### `RES-007` — Chaos exercises end in a safe state, not merely a live process

**VERIFIED** · high · requirement · source: QE-53

Killing the database, killing the WebSocket, delaying and corrupting exchange responses, dropping packets, returning 500s and restarting mid-fill all end in the declared safe state.

- **If violated:** "It did not crash" is mistaken for "it stayed correct".
- **Owned by:** `src/tuning/stress_simulator.py`, `src/tuning/redteam_scheduler.py`, `src/diagnostics/failsafe_policy.py`
- **Verification:**
  - `tests/test_stress_simulator.py` (chaos)
  - `tests/test_redteam_scheduler.py` (chaos)
  - `tests/recovery/test_chaos_suite.py` (chaos) — Each scenario from the source document resolves through the same policy table the runtime consults; compound failures take the strictest answer, and every bounded failure escalates.

#### `RES-008` — Malformed input is rejected safely with an audit event

**VERIFIED** · high · requirement · source: QE-51

Fuzzed API payloads, market data, exchange responses, WebSocket messages, configuration, model inputs and serialised state are rejected with no state corruption, no unauthorized action, no secret leakage, and an audit record.

- **If violated:** A malformed message leaves the system in a state no test ever described.
- **Owned by:** `src/data/quality_gate.py`, `src/execution/exchange_contract.py`
- **Verification:**
  - `tests/fuzz/test_malformed_input_is_safe.py` (fuzz) — Each arrow of the source document's chain is a separate assertion: rejected safely, no corrupted state, no secret leakage, audit event -- not merely 'it did not crash'.

## Release and production

#### `INV-010` — Live mode cannot bypass qualification gates

**VERIFIED** · critical · invariant · source: QE-42,QE-60

Enabling live trading requires every qualification gate to have passed; no configuration flag, environment variable or API call skips one.

- **If violated:** Untested strategy code reaches real capital.
- **Owned by:** `src/risk/gates.py`, `src/risk/paper_qualification.py`, `src/execution/live.py`
- **Verification:**
  - `tests/qualification/test_paper_qualification.py` (security) — LiveExecutor asserts qualification before building any state; the gate takes no override parameter, reads no environment variable, and re-evaluates the stored measurements rather than trusting the stored verdict.

#### `REL-001` — Paper qualification precedes live

**VERIFIED** · critical · requirement · source: QE-60,QE-62

Live trading is unlocked only after the declared paper-trading qualification -- duration, sample count and performance criteria -- has been met and recorded.

- **If violated:** A strategy meets real capital before it has ever met real conditions.
- **Owned by:** `src/risk/gates.py`, `src/risk/paper_qualification.py`
- **Verification:**
  - `tests/test_risk_gate.py` (verification)
  - `tests/qualification/test_paper_qualification.py` (validation) — Duration, sample count and eight performance criteria, all of which must pass; the lucky fortnight fails on sample size however good its numbers, and tightening a threshold invalidates an older pass.

#### `REL-002` — Production is a protected GitHub environment

**VERIFIED** · critical · requirement · source: QE-62,QE-88

Deployment to production requires environment approval, a trusted branch, a trusted workflow and short-lived credentials; pushing to main is not sufficient.

- **If violated:** A merge becomes a deployment with nobody deciding.
- **Owned by:** `.github/workflows`, `.github/workflows/deploy.yml`
- **Verification:**
  - `tests/production/test_production_gate.py` (security) — Every deploying job names a protected environment, promote uses the stricter one, the workflow is dispatch-only, and its token is read-only by default.

#### `REL-003` — Canary precedes full exposure

**VERIFIED** · critical · requirement · source: QE-63

A new release trades at a reduced exposure with monitoring before exposure is increased; there is no paper-to-100% path.

- **If violated:** A regression meets the whole account at once.
- **Owned by:** `src/risk/gates.py`, `.github/workflows/deploy.yml`
- **Verification:**
  - `tests/production/test_production_gate.py` (e2e) — Promotion depends on the canary, the canary exposure is bounded at 25 percent, and promotion is skipped during a rollback drill.

#### `REL-004` — Automatic halt triggers are machine-enforced

**VERIFIED** · critical · requirement · source: QE-64

Unexpected position, reconciliation failure, data staleness, risk-engine failure, authentication anomaly, abnormal order rate, exchange errors, crash loop, model drift and unexpected configuration change each raise an alert, a safe halt and an audit record.

- **If violated:** A human has to notice a problem that a rule could have caught in a second.
- **Owned by:** `src/diagnostics/runtime_monitor.py`, `src/risk/strategy_kill_switch.py`, `src/diagnostics/halt_triggers.py`
- **Verification:**
  - `tests/test_runtime_monitor_coverage.py` (component)
  - `tests/production/test_halt_triggers.py` (resilience) — Each condition fires alone, a compound incident takes the strictest severity rather than the first match, and a trigger whose state is missing or malformed fires instead of passing.

#### `REL-005` — The production startup self-test blocks live on any failure

**VERIFIED** · critical · requirement · source: QE-75

Configuration, secrets, TLS, database, exchange authentication, market-data freshness, model load, risk engine, audit, kill switch, reconciliation and qualification are all checked at startup; any failure means LIVE BLOCKED.

- **If violated:** The bot starts trading with a component that was never actually up.
- **Owned by:** `src/api/main.py`, `src/diagnostics/runtime_monitor.py`, `src/diagnostics/startup_selftest.py`
- **Verification:**
  - `tests/production/test_startup_selftest.py` (verification) — Debug left on, a testnet endpoint, a cleartext endpoint, a percentage where a fraction was meant, a missing secret and an unreachable kill switch each block live; an unevaluated check blocks and stays distinct from a failed one.

#### `REL-006` — Configuration drift is detected and DEBUG never reaches production

**VERIFIED** · critical · requirement · source: QE-74

Unexpected environment variables, risk limits, exchanges, endpoints, debug mode or authentication configuration are detected against a versioned baseline, and DEBUG=true is refused in production.

- **If violated:** A debugging change made at 2am stays in production for a month.
- **Owned by:** `src/config.py`, `src/diagnostics/startup_selftest.py`
- **Verification:**
  - `tests/test_unenforced_config_knobs.py` (verification)
  - `tests/test_env_example_documents_required_settings.py` (verification)
  - `tests/production/test_startup_selftest.py` (verification) — Drift is detected in both directions -- an added key is an unreviewed setting, a removed key is a deleted control -- and debug switching on shows as drift.

#### `REL-007` — A behavioural security layer halts anomalous trading

**VERIFIED** · critical · requirement · source: QE-66

Order frequency, symbol set, size, direction and time-of-day are profiled, and a sharp deviation raises a security alert and halts trading even when the credentials are valid.

- **If violated:** A credential thief drains the account using perfectly valid credentials.
- **Owned by:** `src/diagnostics/runtime_monitor.py`, `src/diagnostics/behavioural_guard.py`
- **Verification:**
  - `tests/production/test_behavioural_guard.py` (security) — Unknown symbol, off-hours activity, bursts, sub-second intervals, outsized orders and long directional runs all halt, while a normal session does not trip the guard. The profile is declared rather than learned.

#### `REL-008` — Rollback is defined and exercised

**VERIFIED** · high · requirement · source: QE-61

The production workflow can return to the previous trusted artifact, and a drill proves it.

- **If violated:** The only way out of a bad deploy is forward.
- **Owned by:** `.github/workflows`, `.github/workflows/deploy.yml`
- **Verification:**
  - `tests/production/test_production_gate.py` (recovery) — A rollback job exists, is exercised by the drill input, verifies the running version changed, and is mutually exclusive with promotion.

## Governance

#### `INV-016` — check_every_gate_status_is_reachable has a negative test

**VERIFIED** · medium · invariant · source: QE-52

The static invariant that refuses an unreachable GateStatus member carries a dedicated fake-tree test proving it can fire: given a synthetic gates.py whose evaluate_all_gates does not call a check that emits a declared status, the invariant reports it.

- **If violated:** The check was reached only by the whole-repo positive gate; a bug that stopped it detecting an unreachable status would ship silently, and a declared halt the stack cannot emit is a risk control that does not exist. HALT_DRIFT was exactly that shape.
- **Owned by:** `scripts/check_static_invariants.py`
- **Verification:**
  - `tests/test_static_invariants.py` (contract)

#### `GOV-001` — Every production defect yields a permanent regression test

**VERIFIED** · high · requirement · source: QE-48

A defect gets a REG-#### entry, a root cause, a test that fails before the fix, and that test is never deleted because the bug is gone.

- **If violated:** The same bug ships twice.
- **Owned by:** `config/quality_registry.json`, `src/quality/registry.py`
- **Verification:**
  - `tests/quality/test_quality_registry.py` (verification) — The registry refuses a regression entry that names no test.

#### `GOV-002` — Every security finding yields a permanent SEC-#### test

**VERIFIED** · high · requirement · source: QE-73

A security weakness gets a SEC-#### entry and a test that would fail if the weakness returned.

- **If violated:** A fixed vulnerability quietly reappears in a refactor.
- **Owned by:** `config/quality_registry.json`, `src/quality/registry.py`
- **Verification:**
  - `tests/quality/test_quality_registry.py` (verification) — Same contract as GOV-001, applied to the security_regression kind.

#### `GOV-003` — Mutation testing runs nightly on the critical subsystems

**VERIFIED** · medium · requirement · source: QE-49

Risk, execution, signals, position sizing and the order FSM are mutation-tested on a schedule, with per-subsystem thresholds.

- **If violated:** Coverage is reported as quality evidence when it is not.
- **Owned by:** `.github/workflows`, `scripts/check_mutation_score.py`
- **Verification:**
  - `tests/regression/test_regression_registry_contract.py` (mutation) — nightly-quality.yml runs one matrix job per subsystem on a schedule; a pull-request gate that slow is one that gets switched off.
  - `tests/test_assert_jobs_green.py` (verification) — The nightly workflow's own gate covers every job in it.

#### `GOV-004` — No silent degradation in a security or risk path

**VERIFIED** · critical · requirement · source: QE-76

A bare except, a broad exception handler, a `pass` in a critical path and a default-allow security decision are all detected by static analysis and individually reviewed.

- **If violated:** A swallowed exception in the risk gate becomes a financial vulnerability.
- **Owned by:** `scripts/check_static_invariants.py`
- **Verification:**
  - `tests/test_static_invariants.py` (verification) — check_no_silent_broad_except catches the `pass` shape; check_no_default_allow_on_failure catches the worse one -- a broad except in a risk, API, security or execution module that returns a permitting value.

#### `GOV-005` — Requirement-to-test traceability is machine-checked

**VERIFIED** · high · requirement · source: QE-5

Every requirement names its verifying tests, those files are checked to exist at load, and the traceability document is generated rather than maintained by hand.

- **If violated:** The matrix says a requirement is covered by a test that was deleted last quarter.
- **Owned by:** `src/quality/registry.py`, `scripts/generate_quality_docs.py`
- **Verification:**
  - `tests/quality/test_quality_registry.py` (verification)
  - `tests/quality/test_quality_docs_in_sync.py` (verification)

#### `GOV-006` — Serious defects get a recorded root-cause analysis

**VERIFIED** · medium · requirement · source: QE-57,QE-58

Each serious defect records what happened, why detection failed, why the existing control failed, why the design permitted it, and the new control that prevents recurrence.

- **If violated:** "Developer made a mistake" is accepted as a root cause and nothing changes.
- **Owned by:** `docs/quality`
- **Verification:**
  - `tests/regression/test_regression_registry_contract.py` (verification) — The template asks all five questions, refuses to stop at a person, requires the layer that should have caught it, and requires the regression test to fail against the pre-fix code.

#### `GOV-007` — Quality metrics are collected and escaped defects are counted per layer

**VERIFIED** · medium · requirement · source: QE-59

Pass rate, branch coverage, mutation score, regression count, escaped defects, vulnerabilities, secret incidents, MTTD, MTTR, deployment failure rate, rollback rate, recovery-test success, model drift, signal drift, data freshness and reconciliation failures are tracked.

- **If violated:** Nobody can say which verification layer keeps letting defects through.
- **Owned by:** `scripts/collect_quality_metrics.py`, `docs/quality`
- **Verification:**
  - `tests/regression/test_regression_registry_contract.py` (verification) — A metric it cannot measure is reported as unavailable with a reason rather than omitted, and zero escaped defects is stated explicitly -- 'we have not measured this' and 'this is zero' are different statements.

#### `GOV-008` — main is protected and every required check must be green

**VERIFIED** · critical · requirement · source: QE-88

Direct push, force push, CI bypass, merging a failing PR and unreviewed critical change are all refused; required checks are the workflow gates, not individual jobs.

- **If violated:** A required check that never reports satisfies the requirement by being absent.
- **Owned by:** `scripts/assert_jobs_green.py`, `scripts/apply_repo_ruleset.py`
- **Verification:**
  - `tests/test_assert_jobs_green.py` (verification)
  - `tests/test_apply_repo_ruleset.py` (verification)

#### `GOV-009` — The quality maturity target is Level 5 on the trading-critical path

**VERIFIED** · medium · requirement · source: QE-85

Supply-chain security, artifact provenance, cryptographic controls, continuous threat monitoring, formal traceability, mutation testing, red-team exercise and independent review all apply to the trading-critical path; non-critical utilities may sit lower, explicitly.

- **If violated:** Maturity is claimed globally on the strength of the easiest subsystem.
- **Owned by:** `docs/quality`, `docs/quality/PRODUCTION_READINESS.md`
- **Verification:**
  - `tests/production/test_production_gate.py` (verification) — Each claimed Level 5 capability names an artifact that exists on disk: supply chain, provenance, cryptographic controls, threat model, recovery, traceability and the readiness conjunction.

#### `GOV-010` — "Production ready" is a conjunction, not a coverage number

**VERIFIED** · critical · requirement · source: QE-87

Production readiness requires every mandatory test, every critical security control, no unresolved critical or high defect, risk invariants, model verification, the regression suite, the recovery test, paper qualification, artifact integrity and production approval -- all of them.

- **If violated:** A single green percentage is mistaken for readiness.
- **Owned by:** `docs/quality`, `scripts/check_production_readiness.py`, `docs/quality/PRODUCTION_READINESS.md`
- **Verification:**
  - `tests/production/test_production_gate.py` (verification) — Readiness reports blockers rather than a percentage, every critical entry must be verified, a claimed test that is missing is a blocker, and an evaluation that cannot run exits 2 rather than being folded into a failure.

#### `GOV-011` — CI output reaching an agent is bounded at 30 lines per fetch

**VERIFIED** · high · requirement · source: OPS-2026-09-20

Every command that can page a file, a history or a CI log into an agent's context must carry an explicit bound of at most 30 lines, and a violation returns exactly one configured instruction line and nothing else.

- **If violated:** An unbounded log fetch exhausts the context window, and the session loses the state it needed in order to fix the failure it was reading about.
- **Owned by:** `.claude/hooks/pre_tool_use.py`, `config/command_policy.json`
- **Verification:**
  - `tests/test_pre_tool_use_hook.py` (unit) — Pins the limit at the inclusive boundary -- 30 allowed, 31 refused -- and asserts the refusal is the single line read from policy, so the message and the configuration cannot drift apart.

#### `GOV-012` — Live CI monitoring is refused, not rate-limited

**VERIFIED** · high · requirement · source: OPS-2026-09-20

Following a CI run -- gh run watch, --watch, tail -f, docker or kubectl logs -f, watch -n, inotifywait -m, a while-true poll loop, or the Monitor tool -- is refused for this project regardless of what it is pointed at.

- **If violated:** A watch with no end emits into context for as long as CI runs, which no per-call line bound can cap.
- **Owned by:** `.claude/hooks/pre_tool_use.py`, `config/command_policy.json`
- **Depends on:** `GOV-011`
- **Verification:**
  - `tests/test_pre_tool_use_hook.py` (unit) — Covers each spelling of a live watch and the Monitor tool, and asserts a bounded log fetch is still allowed, so the rule caps streams without blinding the agent to failures.

#### `GOV-013` — CI failure is pushed to the pull request, never polled for

**VERIFIED** · medium · requirement · source: OPS-2026-09-20

Every completed run of a pull-request workflow posts a single self-updating comment naming each job that is not green -- failure, cancellation, timeout, neutral, action_required, stale, or an unexcused skip -- together with its first failing step and the exact failing lines, so no agent ever reads a run log; a green run posts nothing, and a standing notice is rewritten as recovered.

- **If violated:** Without a push notice the only way to learn a pull request failed is to poll or to read logs, which is exactly what GOV-011 and GOV-012 forbid.
- **Owned by:** `.github/workflows/ci-failure-notify.yml`
- **Depends on:** `GOV-012`
- **Verification:**
  - `tests/test_ci_failure_notify_workflow.py` (unit) — Asserts the job carries no `if:` so every completed run is inspected, that the not-green predicate is an inverted allowlist, that its ALLOW_SKIPPED set equals the union of every gate's, that the notice carries extracted failure messages under a hard line cap, and that it updates its prior comment rather than stacking new ones.

#### `GOV-014` — Every required check also runs in the merge queue

**VERIFIED** · high · requirement · source: OPS-2026-09-20

A workflow that gates a pull request must also trigger on merge_group, and its gate job must not be fenced off by a pull-request-only condition, so that every check branch protection requires can report on the queue ref.

- **If violated:** A required check that does not run on the queue ref never reports, the entry waits for a result that cannot arrive, and the queue jams for every pull request behind it -- silently, because nothing failed.
- **Owned by:** `.github/workflows/ci.yml`, `.github/workflows/codeql.yml`, `.github/workflows/workflow-lint.yml`, `.github/workflows/security.yml`
- **Depends on:** `GOV-013`
- **Verification:**
  - `tests/test_merge_queue_wiring.py` (unit) — Derives the gating set from the workflows themselves rather than restating it, so a newly added gate cannot quietly skip the merge_group requirement.

#### `GOV-015` — A pull request's wall clock is bounded by its tests, not its installs

**VERIFIED** · medium · requirement · source: OPS-2026-09-21

Only the job that runs the test suite installs the project's runtime dependencies; jobs whose steps are stdlib-only install nothing beyond the one tool they invoke, torch is always taken from the CPU wheel index, the suite's install is cached and keyed on the requirements files, the shard count matches the declared split total, and a superseded run on a branch is cancelled.

- **If violated:** An install added to a job that does not need it is invisible in a green run, so minutes accumulate on every pull request with nothing attributing them to the change that added them.
- **Owned by:** `.github/workflows/ci.yml`, `.github/workflows/codeql.yml`
- **Depends on:** `GOV-013`
- **Verification:**
  - `tests/test_ci_workflow_cost.py` (unit) — Asserts no stdlib-only job installs requirements.txt or torch, that the lint job reads its ruff pin from requirements-dev.txt, that the test job installs through uv with its cache enabled, that every workflow installing torch names the CPU index, that the shard list and the declared total agree, and that a new push cancels the previous run off main.

#### `GOV-016` — A new test is written to run as fast as it can while still deciding its question

**VERIFIED** · medium · requirement · source: OPS-2026-09-21

The suite's own running cost is budgeted and the budget only ever falls: the number of real wall-clock sleeps and process spawns in tests/ is frozen and may be lowered but never raised, no single stall exceeds 0.1s, and a budget with slack left in it fails until it is lowered so the saving is banked rather than spent.

- **If violated:** A sleep or a spawn added to one test is imperceptible and is never attributed to the commit that added it, so the suite's cost rises monotonically and the minutes bought back by sharding are given away.
- **Owned by:** `tests/test_suite_speed_budget.py`
- **Depends on:** `GOV-015`
- **Verification:**
  - `tests/test_suite_speed_budget.py` (unit) — Counts sleeps between a scheduler yield and a cancelled-task sentinel and subprocess spawns across every test module, asserts both stay inside their frozen budgets, asserts no single stall exceeds 0.1s, and asserts each budget equals the actual count so slack cannot accumulate.

#### `GOV-017` — The last manual step in the merge path presses itself, one pull request at a time

**VERIFIED** · medium · requirement · source: OPS-2026-09-21

When main moves, exactly one open pull request -- the oldest non-draft one whose mergeable_state is `behind` -- is brought up to date, using a token whose pushes start workflow runs; a missing token fails the job rather than leaving a branch up to date with stale checks, nothing is parked, drafted or closed, and the chain continues when that pull request merges.

- **If violated:** A branch updated with GITHUB_TOKEN starts no workflow run, so the pull request carries the check runs of its previous head: up to date, green-looking and permanently unmergeable -- the same trap the removed queue's promotion step fell into.
- **Owned by:** `.github/workflows/pr-auto-update.yml`
- **Depends on:** `GOV-013`
- **Verification:**
  - `tests/test_pr_auto_update_workflow.py` (unit) — Asserts it fires on a push to main and can be restarted by hand, that two runs cannot pick the same pull request, that it refuses GITHUB_TOKEN and fails before mutating anything when the PAT is absent, that it returns after one update, skips drafts, updates only a `behind` branch, retries an uncomputed mergeability, tolerates a 422 race, and parks or drafts nothing.

#### `GOV-018` — Dependencies between src packages run downward, and the exceptions are a shrinking list

**VERIFIED** · medium · requirement · source: OPS-2026-09-21

Every top-level package in src/ is placed in exactly one layer of config/architecture_layers.json and may import only from its own layer or a lower one; a package in no layer is a failure, each upward edge that already exists is declared with the argument for it, and an inversion that has been fixed must leave the list rather than leaving a slot for the next one.

- **If violated:** A package-level cycle never fails at import time -- it hides behind submodule and deferred imports -- and surfaces instead as two packages that cannot be changed, tested or reasoned about apart.
- **Owned by:** `config/architecture_layers.json`, `scripts/check_static_invariants.py`
- **Verification:**
  - `tests/test_architecture_layers.py` (unit) — Pins the contract -- every package placed once, every layer and every accepted inversion carrying its argument, endpoints that exist on disk, api outermost -- and drives the rule on synthetic graphs: an upward edge is reported, a downward or same-layer one is not, an accepted one is not, a fixed one must be banked, and an unplaced package fails.
  - `tests/test_static_invariants.py` (unit) — test_repository_satisfies_every_invariant runs the layering check against the live repository on every push.

#### `GOV-019` — CI run data is unreadable from a session under every condition

**VERIFIED** · high · requirement · source: OPS-2026-09-22

No CI log, job record, step annotation, artifact, cache entry or check result can be fetched from an agent session, and no output bound, filter or redirect makes it allowed. Dispatching or re-running a workflow to read its output is refused on the same footing. Enforced by common.command_schema.is_ci_log_access at both the PreToolUse hook and shell_exec.run(), with the policy file carrying the same patterns for sessions where the project is not importable.

- **If violated:** Context exhaustion by instalments. The prior policy capped bulk log retrieval instead of banning it -- 'the minimum number of lines that explains the failure is always permitted' -- which put the judgement in the hands of the party that wanted the lines and decayed into paging logs a few lines at a time. A bound cannot fix this because the objection is not output size: the notice comment already carries the status and the exact failing assertion, so a fetch adds nothing and costs a context window.
- **Owned by:** `common/command_schema.py`
- **Depends on:** `GOV-011`
- **Verification:**
  - `tests/test_ci_log_access.py` (contract)
  - `tests/test_pre_tool_use_hook.py` (contract)

#### `GOV-020` — One comment per commit carries the status and the exact failing lines

**VERIFIED** · high · requirement · source: OPS-2026-09-22

The CI notice waits until every watched workflow has completed for a commit, then posts or edits exactly one pull-request comment containing the status and, for each job that is not green, the workflow, job, conclusion, failing step and the exact failing lines extracted server-side. It posts on green as well as on failure, carries nothing beyond status and errors, and filters the runner's own exit-code epilogue out of the error text.

- **If violated:** A contributor or agent with no route to the cause of a red pull request. Since GOV-019 makes CI logs permanently unreadable, this comment is the only channel, so each of its failure modes is total: one comment per workflow means five to read and four chances to read a stale one; posting before every workflow finishes announces green on a commit that failed; and reporting 'Process completed with exit code 1' -- which is what the notice actually said on a failing shard of PR 313 -- names no test, no file and no assertion, leaving nothing to act on at all.
- **Owned by:** `.github/workflows/ci-failure-notify.yml`
- **Depends on:** `GOV-019`
- **Verification:**
  - `tests/test_ci_failure_notify_workflow.py` (contract)
  - `tests/test_ci_log_access.py` (contract)

#### `GOV-024` — intelligence does not import intel

**VERIFIED** · medium · requirement · source: QE-52

src/intelligence/ must not import from src.intel. The adapter takes the concrete CryptoIntelligence via dependency injection, typed as Any at the analytics-layer boundary, so intelligence stays inside its layer.

- **If violated:** A future edit reintroduces an import of src.intel in src/intelligence/, silently reviving the intelligence->intel package edge.
- **Owned by:** `src/intelligence/intelligence_adapter.py`
- **Verification:**
  - `tests/test_architecture_layers.py` (contract)

#### `REG-0005` — A test's result never depends on which tests ran before it

**VERIFIED** · high · regression · source: QE-91

Every test establishes the state it asserts on. No test reads process-global state that an earlier test left behind, so a test's result is identical under any execution order and any pytest-xdist worker assignment.

- **If violated:** Three ensemble-blend tests passed in file order and failed under pytest-xdist, because AutoTuningScheduler.start() leaves risk.ensemble_blend_weight in a process-global registry and effective_risk_settings() then overlays it on top of the config a later test set. The engine blended at 0.15 while the test had configured 0.0, and the test still reported the code it names as correct.
- **Owned by:** `tests/conftest.py`, `src/tuning/registry.py`, `src/tuning/live_overrides.py`
- **Depends on:** `GOV-001`
- **Verification:**
  - `tests/test_signal_engine.py` (verification) — TestEnsembleBlendPersistence pins its own blend weight rather than inheriting whatever the process-wide parameter_registry holds, and test_promoted_registry_weight_overrides_static_cfg asserts the registry overlay those tests previously depended on by accident.
  - `tests/test_tuning_live_overrides.py` (unit) — Pins the overlay itself: a registered value wins over the base settings it is handed.

> Escaped to the test suite, not to a running system: the blend weight the engine used in production was always correct, and what failed was the test's claim to have checked it. Filed as a regression anyway because the rule that a defect gets a permanent test applies to a false green as much as to a bad trade. layer: test-suite

#### `REG-0007` — A CI job installs every third-party module its own steps import

**VERIFIED** · medium · regression · source: OPS-2026-09-21

python-lint installs each third-party module reachable from the scripts it runs -- derived from their imports, not from a list kept by hand -- with every version read out of a requirements file rather than repeated inline.

- **If violated:** Trimming the job to ruff alone left qe_gate without jsonschema, which it raises GateError without, and without PyYAML, which it needs to parse the workflows. The local run passed because the developer's interpreter already had both, so the gap appeared only in CI.
- **Owned by:** `.github/workflows/ci.yml`
- **Depends on:** `GOV-015`
- **Verification:**
  - `tests/test_ci_workflow_cost.py` (unit) — test_the_lint_job_installs_everything_its_steps_import walks the AST of qe_gate.py, both generate_*_docs.py and both registry modules, keeps the imports that are not stdlib, and asserts the job installs each one. Removing jsonschema from the install line fails it, which is the defect that produced the entry.

> Escaped to CI, not to a running system: the gate this broke is what checks the registry, and it broke because the job stopped installing what the gate imports. It passed locally because the developer's interpreter already had jsonschema and PyYAML, so the only environment that could see it was the clean one. Filed because a green local run that is red in CI is a false green, and the rule that a defect gets a permanent test applies to those too. layer: test-suite

#### `REG-0010` — Registry ids are allocated against every branch, not one work tree

**VERIFIED** · high · regression · source: OPS-2026-09-22

The scaffolder must refuse to hand out a registry id that is already used by an entry on any other branch, and must refuse an explicitly passed id that another branch already holds.

- **If violated:** Two branches open at once are handed the same id; on merge two different requirements share it, or one is renumbered and a depends_on elsewhere silently points at the wrong entry.
- **Owned by:** `.claude/skills/quality-engineering/scripts/qe_registry_refs.py`
- **Verification:**
  - `tests/quality/test_qe_id_allocation.py` (regression)

> Happened three times in one session (SECR-013/014 taken by two branches, then a third). Every collision was caught by hand; no check would have caught any of them. The scaffolder had tests, and they asserted that the entry it produced was schema-valid -- never that the id it chose was free anywhere but here. layer: test-suite

#### `REG-0011` — The scaffolder validates with the loader the gate runs

**VERIFIED** · medium · regression · source: OPS-2026-09-22

qe_new_requirement.py must report success only for an entry that src.quality.registry.load_registry accepts, not merely one the JSON schema accepts.

- **If violated:** The scaffolder prints [ok] for an entry the gate rejects, moving the failure to CI and making an invalid entry look reviewed on the way past.
- **Owned by:** `.claude/skills/quality-engineering/scripts/qe_new_requirement.py`
- **Depends on:** `REG-0010`
- **Verification:**
  - `tests/quality/test_qe_id_allocation.py` (regression)

> Observed as '[ok  ] added SEC-0001' for an entry whose test_type was 'governance', which the schema does not constrain and the loader does. Its tests asserted the scaffolder's output validated against the schema, which is the weaker of the two validators it had to satisfy. layer: test-suite


---

## Governance

This document is generated from `config/quality_registry.json`, which is
validated on load by `src/quality/registry.py` and in CI by
`tests/quality/test_quality_registry.py`. The validation is not decorative:

- An entry marked `verified` or `partial` must name at least one test, and
  every named test **must exist on disk**. The registry cannot claim a test
  that was never written or that has since been deleted.
- An entry marked `planned` must name **no** test and **must** name the PR that
  will verify it. Unfinished work is not allowed to sit without an owner.
- An entry marked `accepted_gap` must carry a waiver with a reason, a person
  and a review date. A waiver with no expiry is a permanent hole.
- A `critical` entry can **never** be an `accepted_gap`. To waive it you must
  first argue, in the diff, that it is not critical.
- The id prefix and the declared `kind` are one fact: `INV-` is an invariant,
  `REG-` a regression, `SEC-` a security regression, anything else a
  requirement. A mistyped kind cannot hide an invariant among the requirements.
- Every `depends_on` must resolve, and the dependency graph must be acyclic.

To add or change an entry, edit the registry and regenerate this file. See
`docs/quality/QUALITY_POLICY.md` for the policy these requirements serve,
`docs/quality/TEST_STRATEGY.md` for the taxonomy the `test_type` column draws
on, and `docs/quality/IMPLEMENTATION_PLAN.md` for what each phase delivers.

Registry version: 1.0.0 — 115 entries.
