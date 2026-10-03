# Paper → live promotion checklist

Do not flip `TRADING_MODE=live` in `.env` until every item below is signed
off. The gates in code are necessary but not sufficient — the operational
items around them are the actual bar.

## Code-enforced criteria

Defined in [src/config.py](../../src/config.py) `RiskSettings` and
checked in [src/models/trainer.py](../../src/models/trainer.py) around the
`_live_gate` logic:

- [ ] `oos_sharpe > RISK_OOS_SHARPE_THRESHOLD` (default **1.5**), on
      **out-of-sample** CPCV data — never on the training window.
- [ ] `max_drawdown < RISK_MAX_DRAWDOWN_THRESHOLD` (default **15%**).
- [ ] `n_trades >= RISK_MIN_TRADES_LIVE_GATE` (default **500**).
- [ ] `PAPER_TRADING_DAYS_MINIMUM` reached (default **30** days).

## Operational criteria (not enforced by code)

- [ ] Exchange sandbox / testnet has run the same code path for at least
      7 continuous days without an unexplained crash or kill-switch trip.
- [ ] `GET /debug/reconcile` shows zero divergence between in-memory
      book and persisted trades for a full trading day.
- [ ] `logs/AUTOMATED_DECISION_LOG.md` reviewed — every automated
      structural change (model promotion, strategy retirement) makes
      sense.
- [ ] Capital-preservation floor
      ([src/risk/capital_preservation_floor.py](../../src/risk/capital_preservation_floor.py))
      exercised once in the sandbox (deliberately induce >30% drawdown,
      confirm halt, confirm `re_authorize()` un-halts).
- [ ] Kill-switch wiring
      ([src/risk/strategy_kill_switch.py](../../src/risk/strategy_kill_switch.py))
      exercised — force a strategy past its threshold, confirm it stops
      opening positions.
- [ ] Prometheus scrape is green
      ([deploy/prometheus.yml](../prometheus.yml)) and every alert in
      [deploy/alerts.yml](../alerts.yml) has been fired at least once
      in a drill.
- [ ] Alertmanager destination (Slack / PagerDuty / email) has been
      tested end-to-end.
- [ ] Exchange keys are **live** keys with IP allowlist set,
      `trade`+`read` only, **withdraw disabled**. See
      [KEY_ROTATION.md](KEY_ROTATION.md).
- [ ] Starting capital is small enough that a total-loss event is
      recoverable. Do not begin live with the target size.
- [ ] Runbook ([RUNBOOK.md](RUNBOOK.md)) has been read by whoever is
      on call.
- [ ] Compliance sign-off ([COMPLIANCE_CHECKLIST.md](COMPLIANCE_CHECKLIST.md))
      complete.

## Canary sizing

For the first 7 days of live:

- [ ] `STARTING_CAPITAL_USD` = 1–5% of the intended production size.
- [ ] `RISK_MAX_POSITION_SIZE_PCT` unchanged from defaults.
- [ ] `EXECUTION_MODE=restricted` — every trade above
      `notional_limit_usd` goes to the approval queue.
- [ ] Manual review of every fill for the first 24h.

Only after 7 continuous days of clean canary operation may capital be
scaled up, and only in ≤2x steps with a 48h observation window between
each step.

## Rollback

If any of the following happens in live, revert to paper immediately
(`TRADING_MODE=paper`, restart):

- Any unexplained `GET /debug/reconcile` divergence.
- Kill switch trips without an obvious market-side cause.
- Any auth-failure spike (see `KEY_ROTATION.md` compromise response).
- Cumulative live PnL diverges from same-period paper PnL by >2σ of
  the historical paper-live spread.

Rollback is cheap. Take it.
