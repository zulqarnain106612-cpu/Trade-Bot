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

## Risk-003 [2026-06-29] — NEW (audit session, Amazon Q)
Disconnected risk/intelligence gate layer — operators may believe more protection exists
than actually does.

The probabilistic_gates.py, intelligence_gates.py, and portfolio_correlation.py modules
all exist, all have passing unit tests, and are described (implicitly via SESSION_STATE.json
"COMPLETE" claims) as active runtime protection. None of them are reachable from the live
signal path as of this audit (Gap-015, Gap-017).

Impact: An operator reviewing the codebase before going live sees a CognitiveEngine with
5 validators, a probabilistic gate layer, an intelligence gate layer, and a portfolio
correlation tracker — and reasonably concludes the system has multi-layered protection.
In reality only the CognitiveEngine's 5 validators run. The other layers provide zero
runtime protection.

This is not just a code quality issue — it is an operator decision-making risk. If a
human signs off on live trading based on an architecture review that assumes these layers
are active, they are making that decision on false information.

Severity: High (decision-making risk before live gate approval)
Mitigation required before live: Either wire all claimed-active modules into the signal
path and add integration tests proving they affect real trade decisions, OR produce a
clear written statement in OPERATOR_RUNBOOK.md that explicitly lists which modules are
ACTIVE vs EXPERIMENTAL, so the human reviewer knows what is and isn't protecting them.
Reported by: Amazon Q [amazonq]
────────────────────────────────────────────────────────────

## Risk-004 [2026-06-29] — NEW (audit session, Amazon Q)
Critical-path test coverage subsidy — the 60% coverage gate passes while live.py has 29%
coverage and orchestrator.py has 10% coverage.

The single repo-wide coverage floor can be satisfied by high-coverage utility modules
(storage.py 96%, kelly.py 96%, slippage.py 100%) while the two files that actually place
real orders and drive the event loop remain severely under-tested. This means the CI gate
that is supposed to prevent regressions on the live trading path provides materially less
protection than the 60% number suggests.

Impact: A regression in live.py's order placement logic (e.g. wrong side direction, wrong
quantity calculation, wrong FSM state transition) could pass all CI checks and reach
production without a single test catching it.

Severity: High (safety-critical path protection gap)
Mitigation: Add per-package coverage floors in pyproject.toml — specifically src/execution/
and src/engine/ should have a separate minimum (recommend 70%+). The global gate alone is
insufficient for a system that places real financial orders.
Reported by: Amazon Q [amazonq]
────────────────────────────────────────────────────────────
