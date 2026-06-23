# Architecture Gaps
> Auto-maintained by Project Intelligence Router
> Agents: read this file for known issues before implementing

## Gap-001 [2026-06-23] — RESOLVED [2026-06-23]
No slippage + market impact model in live.py.
Almgren-Chriss model needed: slippage_bps = spread + impact * sqrt(qty/adv_20d).
Severity: High. File: src/execution/live.py, src/risk/
Status: RESOLVED — src/risk/slippage.py (SlippageModel.estimate / veto_if_negative_ev),
wired into src/risk/gates.py as gate 0 (check_slippage_veto). Settings added to
RiskSettings (slippage_default_spread_bps, slippage_impact_coeff_bps,
slippage_veto_margin_bps). 18 tests, 100% line coverage on the new module.
NOTE: live.py execution itself is not yet calling SlippageModel directly —
the gate exists and fails open (passes) until a call site supplies an
expected_edge_bps + SlippageEstimate. Wiring the signal engine / live
executor to actually populate these fields is tracked as a new follow-up
task (see OPEN_TASKS.md).
────────────────────────────────────────────────────────────

## Gap-002 [2026-06-23] — RESOLVED (verified 2026-06-23, session 2)
GaussianHMM regime has no posterior entropy gate.
hmm.predict_proba() entropy not computed — confidence not quantified.
Risk: regime misclassification during transitions blocks or opens positions incorrectly.
Severity: High. File: src/regime/detector.py
Status: RESOLVED — implemented in commit 99ad2a5 (entropy field on
RegimePrediction, normalized Shannon entropy, position_scalar() helper) and
wired into src/risk/kelly.py compute_position_size(regime_scalar=...).
CORRECTION: SESSION_STATE.json from the previous session still listed this
as "NOT STARTED" and as the next recommended task — that was stale. Verified
directly against source (src/regime/detector.py lines ~104-541) before
relying on it. SESSION_STATE.json corrected in this session.
Test backfill (session 3): tests/test_detector.py added (32 tests, 31
passed/1 skipped) — detector.py coverage 0% → 92%. Covers entropy math,
position_scalar() continuity/monotonicity, fit/predict/save/load, and the
non-convergence fail-safe path.

## Gap-007 [2026-06-23] — RESOLVED (session 3, same session it was introduced)
CognitiveEngine (src/risk/cognitive_engine.py, added session 2 alongside
the RAG/intel layer, commit 88a5e73 area) computed position size via an
INDEPENDENT continuous-Kelly formula (_base_size(): mu/sigma^2) completely
decoupled from kelly_result.adjusted_fraction — the real, already
entropy-gated half-Kelly fraction from src/risk/kelly.py. signal_engine.py
then rescaled kelly_result by adjusted_size_fraction / kelly_result.adjusted_fraction,
a ratio between two unrelated formulas with no risk-coherent meaning.
Verified numerically: a realistic scenario (p_long=0.62, edge=24bps,
vol=45%, entropy-gated kelly_fraction=0.0784) produced a rescale ratio of
2.23x — the "mandatory risk governor" layer could INFLATE position size
beyond what the entropy gate intended, not just shrink it. This is the
opposite of "never bypass / never weaken a risk gate."
Severity: Critical (money-sizing correctness on every trade in the live
signal path). File: src/risk/cognitive_engine.py, src/engine/signal_engine.py
Status: RESOLVED — CognitiveEngine.evaluate() now starts size_fraction
from a new SignalContext.kelly_adjusted_fraction field (populated from
kelly_result.adjusted_fraction in signal_engine.py) instead of recomputing.
The old continuous-Kelly formula is kept as _continuous_kelly_estimate(),
used ONLY by a new _log_size_divergence() diagnostic (logs WARN if the two
formulas disagree by >50% — useful as a model-calibration signal, never
mutates size). adjusted_size_fraction is now provably bounded to
[0, kelly_adjusted_fraction] — pure multiplicative shrink via WARN (0.70x
each) or zero via VETO, matching the explicit intent that the five
cognitive domains be a mandatory governor on the real trade, not a second
competing estimate.
Also fixed in the same pass: a stale `regime_conf` variable reference
(NameError on the ProbabilityValidator VETO branch) left over from an
interrupted prior edit that renamed the Bayesian-score variables to
dominant_prob/direction_conf.
Tests: tests/test_cognitive_engine.py added (42 tests, all passing,
cognitive_engine.py coverage 0% → 97%), including two targeted regression
tests (test_size_fraction_starts_from_kelly_adjusted_fraction_not_recomputed,
test_size_fraction_never_exceeds_kelly_adjusted_fraction) that will fail
loudly if this regresses.
────────────────────────────────────────────────────────────
────────────────────────────────────────────────────────────

## Gap-003 [2026-06-23]
KS-test drift detection misses label shift.
SignalDebugger detects covariate shift but not feature→return relationship change.
Rolling performance-based trigger needed alongside KS test.
Severity: Medium. File: src/diagnostics/signal_debugger.py
Status: OPEN
────────────────────────────────────────────────────────────

## Gap-004 [2026-06-23]
No order state machine in live executor.
No FSM tracking PENDING→SUBMITTED→PARTIAL_FILL→FILLED→CLOSED|REJECTED|TIMEOUT.
Partial fills corrupt Kelly denominator (position size vs intended size diverge).
Severity: High. File: src/execution/live.py
Status: OPEN
────────────────────────────────────────────────────────────

## Gap-005 [2026-06-23]
No portfolio correlation layer for multi-symbol operation.
Kelly sizing per-symbol ignores cross-asset correlation — correlated drawdowns
breach 2% daily halt faster than per-symbol calculations predict.
Severity: Medium. File: src/risk/ (new file needed)
Status: OPEN
────────────────────────────────────────────────────────────

## Gap-006 [2026-06-23]
SQLite WAL write contention risk at scale.
Under 3 timeframes + audit log + equity updates, SQLite serializes writes.
Migration path to TimescaleDB/QuestDB needed before multi-symbol live.
Severity: Low (paper/single-symbol fine). File: src/data/storage.py
Status: OPEN
────────────────────────────────────────────────────────────
