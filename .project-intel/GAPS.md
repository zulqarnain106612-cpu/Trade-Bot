# Architecture Gaps
> Auto-maintained by Project Intelligence Router
> Agents: read this file for known issues before implementing

## Gap-001 [2026-06-23]
No slippage + market impact model in live.py.
Almgren-Chriss model needed: slippage_bps = spread + impact * sqrt(qty/adv_20d).
Severity: High. File: src/execution/live.py, src/risk/
Status: OPEN
────────────────────────────────────────────────────────────

## Gap-002 [2026-06-23]
GaussianHMM regime has no posterior entropy gate.
hmm.predict_proba() entropy not computed — confidence not quantified.
Risk: regime misclassification during transitions blocks or opens positions incorrectly.
Severity: High. File: src/regime/detector.py
Status: OPEN
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
