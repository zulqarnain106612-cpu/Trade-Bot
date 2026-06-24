# Open Tasks — Prioritized

## P0 — Pre-live requirements

### TASK-001: Slippage + market impact model [GAP-001] — DONE 2026-06-23
**File**: src/risk/slippage.py (new)
**What**: Almgren-Chriss model — expected_slippage_bps = spread_bps + impact_coeff * sqrt(qty / adv_20d)
**Why**: Market orders on Binance have real spread + impact costs; Kelly without this overstates edge
**Implemented**: SlippageModel.estimate() / veto_if_negative_ev(), wired into gates.py as
gate 0 (check_slippage_veto). 18 tests, 100% coverage on the module.
**Follow-up (new, P0)**: see TASK-009 — no call site yet populates
expected_edge_bps/slippage_estimate, so gate 0 currently fails open in
production. Live trading must not be unlocked until TASK-009 closes this.

### TASK-002: HMM posterior entropy gate [GAP-002] — DONE (verified 2026-06-23)
**File**: src/regime/detector.py
**What**: hmm.predict_proba() → compute entropy → if entropy > threshold → position scalar *= 0.5
**Why**: HMM gives state but not confidence; regime transitions are highest-risk moments
**Change**: detector.py predict() should return (state, confidence, entropy) not just state
**Wire into**: signal_engine.py — apply entropy scalar before sizing
**Note**: was implemented in a prior session (commit 99ad2a5) but SESSION_STATE.json
incorrectly still listed it as NOT STARTED. Corrected after verifying against source.

**Status update (session 3)**: wiring confirmed complete — signal_engine.py line ~300 calls regime.position_scalar() and passes it as compute_position_size(regime_scalar=...). Added tests/test_detector.py (32 tests) and a regime_scalar edge-case suite in tests/test_kelly.py (6 tests). TASK-002 fully closed, no follow-up.

### TASK-009: Wire SlippageModel into signal_engine.py + live executor [GAP-001 follow-up] — DONE [2026-06-24]
**File**: src/engine/signal_engine.py, src/execution/live.py
**What**: Compute adv_20d from storage (bars table, 20-day rolling volume),
call SlippageModel.estimate() with live order-book spread, compute
expected_edge_bps from p_long/meta-label + win/loss ratio, populate
RiskGateContext.expected_edge_bps / slippage_estimate before evaluate_all_gates().
**Why**: Gate 0 exists but fails open until this lands — currently no
protective effect in the actual trading path.
**Priority**: P0 — DONE. Gate 0 now active: expected_edge_bps from avg_win/avg_loss model,
SlippageModel.estimate() called with live spread_bps + adv_20d. RiskGateContext fields populated.
502/502 tests pass.

### TASK-010: Wire live order-book spread into CognitiveEngine — DONE [2026-06-24] (funding_rate_8h deferred: spot-only)
**File**: src/engine/signal_engine.py
**What**: SignalContext.spread_bps is hardcoded to 2.0 and funding_rate_8h
to 0.0 at the construction call site (~line 475-480). BlockchainValidator's
funding/basis VETO checks and the slippage participation-rate WARN are
currently evaluating against placeholder constants, not live exchange data.
**Why**: CLAUDE.md prohibits placeholder code in production paths; these
two values directly gate VETO/WARN decisions in the mandatory cognitive
layer, so a hardcoded spread_bps=2.0 means the funding/basis/liquidity
checks are not actually protective in the live signal path yet — same
class of gap as TASK-009 (slippage gate exists but fails open until wired).
**Priority**: P0 — block live-gate unlock on this alongside TASK-009.

### TASK-011: Audit src/strategies/filters.py "scalar" interaction with regime_scalar [NEW]
**File**: src/strategies/filters.py, src/engine/signal_engine.py
**What**: signal_engine.py applies _filter_result["scalar"] to kelly_result
notional/quantity (line ~433) AFTER compute_position_size() already applied
regime.position_scalar() at creation time, and BEFORE CognitiveEngine's
WARN multipliers run on the result. Confirmed CognitiveEngine no longer
double-counts regime entropy (Gap-007 fix), but filters.py's own "scalar"
source/meaning has not yet been read/audited this session — unclear if it
overlaps with regime confidence (e.g. Hurst-based) or is a distinct signal
(e.g. strategy-filter confidence). Needs the same divergence-or-redundancy
check Gap-007 received before assuming it's safe.
**Why**: Same risk class as Gap-007 — multiple uncoordinated scalars on
the same trade can compound or invert in non-obvious ways.
**Priority**: P1.

## P1 — Reliability improvements

### TASK-003: Performance-based model degradation trigger [GAP-003]
**File**: src/diagnostics/signal_debugger.py
**What**: Rolling 50-trade accuracy + rolling 100-trade Sharpe; trigger retrain alert if below threshold
**Why**: KS-test catches covariate shift but not label shift (feature→return relationship change)
**Threshold**: accuracy < 0.52 OR rolling_sharpe < 0.8 → alert + tighten meta-label threshold to 0.65

### TASK-004: Order state machine in live executor [GAP-004]
**File**: src/execution/ (new file: order_fsm.py)
**States**: PENDING → SUBMITTED → PARTIAL_FILL → FILLED → CLOSED | REJECTED | TIMEOUT
**Why**: Binance market orders can partially fill; without FSM position size vs intended size diverges
**Wire into**: live.py — all order placement goes through FSM

## P2 — Scale preparation

### TASK-005: Portfolio correlation layer [GAP-005]
**File**: src/risk/ (new file: correlation.py)
**What**: Compute portfolio beta vs BTC benchmark; reduce Kelly when beta > 1.3
**Why**: Multi-symbol correlated drawdown can breach 2% daily halt faster than per-symbol Kelly predicts

### TASK-006: Storage migration to TimescaleDB [GAP-006]
**File**: src/data/storage.py
**What**: Replace SQLite with asyncpg + TimescaleDB hypertables
**Why**: SQLite WAL serializes writes; under 3 timeframes + audit log = lock contention at scale

## P3 — Intelligence enhancements

### TASK-007: Prometheus metrics endpoint
**File**: src/api/main.py
**What**: GET /metrics → Prometheus format for Grafana dashboarding
**Metrics**: signal_score, regime_state, kelly_fraction, gate_pass_rate, model_accuracy_rolling

### TASK-008: Online learning hook
**File**: src/models/ (new file: online_trainer.py)
**What**: river or vowpal wabbit for incremental model updates without full retrain
**Why**: XGBoost batch retrain is expensive; online learning catches drift faster
