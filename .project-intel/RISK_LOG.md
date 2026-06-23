# Risk Assessment Log
> Auto-maintained by Project Intelligence Router
> Agents: append new risks using <risk> tags via output router

## Risk-001 [2026-06-23]
Live trading without slippage model (GAP-001).
Impact: Kelly bet-size overestimates edge by 1-5 bps per trade.
At 500 trades/month: cumulative drag of 5-25 bps on monthly return.
Mitigation: Do not go live until GAP-001 resolved.
Severity: High
────────────────────────────────────────────────────────────

## Risk-002 [2026-06-23]
HMM regime misclassification during market transitions (GAP-002).
Impact: volatile→trending transition may be misread, allowing position
opening during high-risk period or blocking during momentum window.
Mitigation: Add entropy gate (GAP-002) before live trading.
Severity: High
────────────────────────────────────────────────────────────
