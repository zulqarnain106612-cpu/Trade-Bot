# Risk Assessment Log
> Auto-maintained by Project Intelligence Router
> Agents: append new risks using <risk> tags via output router

## Risk-001 [2026-06-23] — PARTIALLY MITIGATED 2026-06-23
Live trading without slippage model (GAP-001).
Impact: Kelly bet-size overestimates edge by 1-5 bps per trade.
At 500 trades/month: cumulative drag of 5-25 bps on monthly return.
Mitigation: SlippageModel + gate 0 implemented (src/risk/slippage.py,
src/risk/gates.py). STILL OPEN: gate 0 fails open until signal_engine.py /
live.py actually populate expected_edge_bps + SlippageEstimate (TASK-009).
Do not go live until TASK-009 closes — the model existing is not the same
as the model protecting the live path.
Severity: High (downgrade to Medium only after TASK-009 lands)
────────────────────────────────────────────────────────────

## Risk-002 [2026-06-23] — MITIGATED (verified 2026-06-23)
HMM regime misclassification during market transitions (GAP-002).
Impact: volatile→trending transition may be misread, allowing position
opening during high-risk period or blocking during momentum window.
Mitigation: Entropy gate implemented in src/regime/detector.py and wired
into src/risk/kelly.py via regime_scalar. Verified directly against source
(was incorrectly still flagged open in a stale SESSION_STATE.json).
Severity: Low (residual — entropy threshold/floor are defaults, not yet
calibrated against live data; recalibrate once paper trading produces
regime-transition samples)
────────────────────────────────────────────────────────────
