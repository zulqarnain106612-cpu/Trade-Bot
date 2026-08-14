# Trade-Bot Long-Range Roadmap (v1 → v10)

Model: each version is a **major capability epoch**, not a slice of one feature.
A version only "completes" when it is operational in paper/live trading with
0 outstanding correctness issues and passes the full gate
(`ruff` + `mypy` + `pytest -x -q` + coverage floors). Only then does work on
the next version begin. No parallel version work, no partial ship.

Current baseline (v1, already largely built): XGBoost + HMM regime detection,
Kelly-ceiling risk, Binance/OKX/Bybit providers via ccxt, FastAPI orchestrator,
paper + live execution with FSM order management, on-chain intel (Glassnode-
style), portfolio correlation, performance-drift and cognitive-engine risk
modules. Single-account, single-process, momentum/regime-conditioned strategy
set.

---

## v2 — Multi-Strategy Portfolio Engine
Move from "one model votes" to a portfolio of uncorrelated strategies with
capital allocated by realized edge, not code priority.
- Strategy registry + interface contract (signal, confidence, regime-fit,
  required capital) so strategies plug into `src/strategies/` without
  touching the orchestrator.
- Add 3–5 net-new strategy families beyond current momentum/regime signal:
  mean-reversion (stat-arb pairs on correlated majors), volatility carry
  (funding-rate harvesting on perps), breakout/volume-profile, and a
  cross-sectional momentum ranker across the traded universe.
- Strategy-level Kelly + portfolio-level correlation cap
  (`src/risk/portfolio_correlation.py` already has the primitive — extend to
  strategy-vs-strategy, not just asset-vs-asset).
- Per-strategy P&L attribution and kill-switch (auto-disable a strategy whose
  realized Sharpe drops below a rolling threshold — hooks into
  `performance_drift.py`).

**Data/knowledge needed:** historical OHLCV + funding-rate history per
exchange (already fetchable via ccxt), pairs cointegration study for
mean-reversion candidates, walk-forward out-of-sample validation harness.

---

## v3 — Multi-Exchange, Multi-Account Execution
Scale from "trade on N exchanges" to "trade as one logical book across N
exchanges/accounts with unified risk."
- Unified position/margin ledger across Binance, OKX, Bybit (providers exist;
  execution/risk currently assume single book).
- Smart order routing: pick venue per order by liquidity/fee/funding, not
  hardcoded.
- Cross-exchange arbitrage detector (basis, funding-rate spread) as a new
  strategy family feeding v2's registry.
- Exchange failover: if one venue's REST/WS drops, reroute pending orders,
  do not blind-spot risk state.

**Data/knowledge needed:** per-exchange fee schedules, margin/leverage rules,
WS reconnect/backoff semantics per ccxt exchange, existing FSM
(`order_fsm.py`) extended with a venue dimension.

---

## v4 — Adaptive Regime & Model Layer (beyond HMM)
Current regime detector is a single HMM. Upgrade to an ensemble that adapts
to non-stationarity instead of a fixed model.
- Regime ensemble: HMM + change-point detection (e.g. Bayesian online
  changepoint) + volatility-regime clustering, combined via a meta-model
  instead of hard switches (Domain Prior: HMM transitions are probabilistic).
- Online/incremental retraining pipeline for XGBoost models (currently
  presumably batch) — rolling-window retrain with drift-triggered retrain,
  not fixed schedule.
- Feature store versioning (`src/features/`) so model inputs are
  reproducible and auditable across retrains.
- Model registry with shadow-mode evaluation: new model runs in parallel,
  promoted only after N days of out-of-sample outperformance.

**Data/knowledge needed:** longer historical dataset for changepoint model
validation, MLflow-style (self-hosted, free-tier per cost policy) experiment
tracking, existing `src/regime/detector.py` and `src/models/` as the
extension point.

---

## v5 — Derivatives & Structured Strategies
Move beyond spot/perp directional trading into instruments that let the bot
express non-directional views and hedge tail risk.
- Options strategies where available (Deribit/OKX options): covered calls /
  cash-secured puts on core holdings, volatility-skew signals.
- Basis trading (spot-perp, calendar spreads) as a first-class strategy.
- Tail-hedge overlay: small always-on hedge sized from portfolio VaR/CVaR,
  funded by carry from other strategies.
- Extend risk engine (`src/risk/kelly.py`, `gates.py`) with Greeks-aware
  sizing (delta/vega exposure caps), not just notional/Kelly.

**Data/knowledge needed:** options chain data provider (free/paper-tier
first per cost policy), implied-vol surface construction, CVaR estimation
methodology.

---

## v6 — Autonomous Research & Strategy Discovery
The bot stops depending on manually authored strategies and starts
generating/testing hypotheses itself under human sign-off.
- Automated feature/signal search (genetic programming or systematic
  factor-mining) over the existing feature store, with strict
  walk-forward + multiple-testing correction (avoid overfitting bias).
- Auto-generated strategy candidates go through a paper-trading gauntlet
  (min N trades, min N days, Sharpe/drawdown gates) before promotion to the
  v2 strategy registry — never auto-promoted straight to live capital.
- Explainability layer: every promoted strategy ships with a feature-
  importance and regime-conditioned performance report for human review.

**Data/knowledge needed:** significantly larger historical dataset (multi-
year, multi-asset) for factor mining without overfitting; compute budget for
search (respect cost policy — free/local compute first, e.g. local GPU/CPU
grid search before any paid compute).

---

## v7 — Portfolio-Level Macro & Cross-Asset Overlay
Extend the bot's opinion beyond crypto microstructure into macro-conditioned
sizing.
- Ingest macro/on-chain regime signals (funding-rate cycles, stablecoin
  supply growth, exchange netflows — extends `src/intelligence/onchain/`)
  into a macro regime classifier that scales aggregate exposure (risk-on/
  risk-off), separate from the per-trade HMM regime.
- Cross-asset correlation with traditional macro proxies (DXY, rates, equity
  vol) where free data permits, to reduce crypto-only blind spots.
- Dynamic leverage/exposure budget that expands/contracts with macro regime
  confidence, layered on top of Kelly ceiling (still a ceiling, never a
  target — Domain Prior preserved).

**Data/knowledge needed:** free-tier macro data feeds (per cost policy — no
paid Glassnode/Bloomberg until paper-trading proves the earlier versions),
netflow/stablecoin data via existing on-chain provider pattern.

---

## v8 — Institutional-Grade Operations & Compliance
Shift from "one operator's bot" to a system that can run unattended for long
stretches with audit-grade guarantees.
- Full audit trail: every order, signal, and risk decision logged
  immutably with reason codes (extends `src/diagnostics/`).
- Disaster recovery: state snapshot/restore so a crash mid-position
  reconciles against exchange truth on restart, not local assumption.
- Multi-operator access control (read-only monitoring vs. trade-authorizing
  roles) on the FastAPI layer (`src/api/main.py`).
- Formal incident-response runbook automation (ties into
  `engineering:incident-response` skill patterns) — auto-generated postmortem
  scaffolding on any risk-gate breach or execution anomaly.

**Data/knowledge needed:** none external — this is process/infra hardening
of existing `src/execution/`, `src/risk/gates.py`, `src/diagnostics/`.

---

## v9 — Self-Optimizing Capital Allocation
Treat strategies, exchanges, and even model versions as a meta-portfolio
optimized continuously, not just risk-capped.
- Meta-allocator: reinforcement-learning or Bayesian-optimization-based
  capital allocation across the v2 strategy registry, v3 venues, and v4
  model variants — replacing static/manual weighting.
- Continuous walk-forward re-evaluation loop: allocation shifts are
  themselves rate-limited and risk-gated (never let the allocator itself
  become an unbounded risk source).
- Scenario/stress-test simulator (historical crash replay: 2018, 2020,
  2022 crypto crashes) run nightly against current allocation to surface
  tail risk before it's live.

**Data/knowledge needed:** historical crisis-period data for stress
simulator, RL/Bayesian-opt library selection consistent with pip/
requirements.lock constraint (no `uv add`, per existing memory note).

---

## v10 — Fully Autonomous Multi-Decade Operation
The terminal state for this roadmap horizon: a system designed to run with
minimal human intervention over a multi-year/decade horizon, self-auditing
its own long-term edge decay.
- Long-horizon strategy-decay detection: statistically distinguishes
  "regime shift" from "alpha has structurally decayed" and retires/replaces
  strategy families accordingly (builds on v4's drift detection + v6's
  research pipeline, now fully closed-loop).
- Self-updating documentation/decision log: every structural change to
  strategy mix, risk limits, or venues auto-appends to `DECISION_LOG.md`
  with the quantitative justification, so a human auditor can reconstruct
  "why" for any period in the bot's history without tribal knowledge.
- Formal capital-preservation floor: hard, code-enforced (not just
  documented) maximum drawdown that halts all trading and requires explicit
  human re-authorization to resume — the final backstop beneath all nine
  prior versions' automation.
- Periodic (e.g. annual) full-system red-team: replays v9's stress simulator
  against the then-current live system as a standing operational ritual,
  not a one-time audit.

**Data/knowledge needed:** none new — this version is the closed-loop
integration of every prior version's outputs (v4 drift, v6 research
gauntlet, v9 stress simulator) into one self-governing lifecycle.

---

## Sequencing Rule
1. Work only on the active version. A version is "done" when: full gate
   passes, coverage floors hold, it has run in paper trading with zero
   correctness issues for the agreed evaluation window, and no open
   TODO/FIXME remains in the touched modules.
2. On completion, append a dated entry to `DECISION_LOG.md` (create if
   absent) recording what shipped and the evaluation evidence, then move to
   the next version.
3. Do not start version N+1 work while version N has open issues — this
   roadmap is sequential by design, matching the "0 improvements needed"
   gating the user specified.
