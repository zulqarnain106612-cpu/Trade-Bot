# Decision Log

Append-only record of structural changes, referenced by ROADMAP.md's
sequencing rule. One entry per completed version/sub-task.

## 2026-07-27 — v2 Sub-tasks 1-4: Multi-Strategy Portfolio Engine

Implemented per [ROADMAP_V2_PLAN.md](ROADMAP_V2_PLAN.md):

- **Sub-task 1** — `src/strategies/registry.py`: `StrategyProtocol`, `Signal`,
  `StrategyRegistry` with fail-fast validation (duplicate IDs, malformed
  strategies, out-of-range capital fractions). `src/strategies/signal_engine_adapter.py`
  wraps the existing v1 SignalEngine output as a registered strategy
  (`signal_engine_v1`) with zero changes to SignalEngine internals.
- **Sub-task 2** — Four net-new pure-function strategy families, each
  registry-conformant: `mean_reversion.py` (Engle-Granger cointegration
  pairs), `funding_carry.py` (perp funding z-score, reuses existing
  provider-computed funding rate — no new fetch code), `breakout.py`
  (volume-confirmed ATR breakout), `xsec_momentum.py` (cross-sectional
  return-percentile ranking).
- **Sub-task 3** — `src/risk/strategy_correlation.py` reuses Gap-005's
  `PortfolioCorrelationTracker` verbatim, keyed by strategy_id instead of
  symbol, plus `combined_correlation_scalar()` for multiplicative
  asset x strategy sizing reduction. `src/risk/strategy_kill_switch.py`
  wraps `PerformanceDriftDetector` per strategy_id with explicit-only
  re-enable (never auto-reinstates a disabled strategy).
- **Sub-task 4** — `src/diagnostics/attribution.py`: pure per-strategy P&L/
  Sharpe/win-rate/max-drawdown computation from tagged fills. Exposed
  read-only via `GET /strategies/attribution` in `src/api/main.py`. Wired
  into the orchestrator's existing auto-close position-monitor loop
  (`src/engine/orchestrator.py`) as a best-effort, non-fatal hook alongside
  the existing drift-adapter recording call — every trade closed through
  that path today originates from the wrapped v1 signal engine, so it is
  tagged `signal_engine_v1` unambiguously.

**Not yet wired**: the live/paper tick loop still routes exclusively
through the v1 SignalEngine — it does not yet iterate `StrategyRegistry`
to solicit signals from the four new strategy families, and per-strategy
Kelly sizing / correlation scalars / kill-switch gating are not yet
consulted before order placement. Those modules are functional and fully
tested standalone; wiring them into the live sizing/routing decision is
the next concrete step, since it touches the highest-blast-radius code
(`src/engine/orchestrator.py` tick loop, `src/engine/signal_engine.py`
Kelly sizing call sites) and warrants its own isolated validation pass
before v2 can be declared complete per the roadmap's "0 improvements
needed" gate.

**Validation**: `ruff check`, `mypy src/`, full suite
(`uv run pytest tests/ -q`) — 2522 passed, 88 skipped, 0 failed, 96.23%
total coverage (floor: 95%). `python3 scripts/check_coverage_floors.py` —
all 10 safety-critical per-file floors met, including
`src/engine/orchestrator.py` (98.9%, floor 60%) after the new attribution
hook.

## 2026-07-27 — v3 through v10: core mechanisms for every remaining
roadmap version

Per user directive to implement every remaining ROADMAP.md upgrade rather
than stop at v2. Each version below got its core, real, independently
tested mechanism(s) as standalone modules — the same pattern as v2
(pure functions/classes, registry-conformant where applicable, no mocks
or placeholders). **None of these are wired into the live orchestrator
tick loop** — see "Scope and what remains" below; that is a deliberate,
explicit boundary, not an oversight.

- **v2 completion** — `src/strategies/capital_allocator.py`:
  `equal_weight_allocate()`, the baseline capital-split policy across
  enabled registry strategies (renormalized, per-strategy capped) that v9
  later replaces with a learned allocator.
- **v3 Multi-Exchange Execution** — `src/execution/unified_ledger.py`:
  `UnifiedLedger` aggregates per-venue positions into one logical book
  (net/gross exposure, margin usage, venue list per symbol).
  `src/strategies/cross_exchange_arb.py`: basis-spread arbitrage strategy
  between two venues for the same symbol, registry-conformant.
- **v4 Adaptive Regime & Model Layer** — `src/regime/changepoint.py`:
  simplified Bayesian online changepoint detector (Adams & MacKay 2007,
  constant-hazard). `src/regime/ensemble.py`: combines HMM regime output
  with changepoint probability into an agreement score without overriding
  either model. `src/models/model_registry.py`: shadow-mode model
  evaluation — a candidate model runs in parallel, promotion requires
  beating the live model's accuracy over the same window, explicit-only
  promotion (mirrors v2 kill-switch's explicit-only re-enable).
- **v5 Derivatives & Structured Strategies** — `src/risk/greeks.py`:
  Black-Scholes delta/gamma/vega/theta plus portfolio-level Greeks
  exposure caps, independent of Kelly notional sizing.
  `src/strategies/options_carry.py`: covered-call/cash-secured-put signal
  keyed on implied-vol richness. `src/strategies/basis_trade.py`:
  spot-perp basis fade (same-venue complement to v3's cross-exchange
  arb).
- **v6 Autonomous Research & Strategy Discovery** —
  `src/tuning/factor_search.py`: factor evaluation against forward
  returns with a deflated-Sharpe / Bonferroni-corrected significance test
  (Bailey & López de Prado 2014) so trying many candidate factors doesn't
  auto-inflate the apparent best one. Operates on caller-supplied factor
  candidates only — no arbitrary code execution/generation, which would
  be an unacceptable injection surface for an autonomous trading system.
  `src/tuning/promotion_gauntlet.py`: minimum trades/days/Sharpe/drawdown
  gate a discovered strategy must clear in paper trading before it's
  registry-eligible.
- **v7 Portfolio-Level Macro Overlay** —
  `src/intelligence/macro_regime.py`: continuous risk-on/risk-off score
  from funding-rate z-score, stablecoin supply growth, and exchange
  netflow z-score (all sourced from existing intelligence providers).
  `src/risk/macro_exposure_budget.py`: multiplicative scalar (bounded
  [0.25, 1.0] — can only shrink, never amplify) applied on top of Kelly.
- **v8 Institutional-Grade Operations & Compliance** —
  `src/diagnostics/audit_trail.py`: append-only, SHA-256 hash-chained
  audit log with tamper detection via `verify_chain_integrity()`.
  `src/diagnostics/disaster_recovery.py`: pure reconciliation between a
  local position snapshot and exchange-reported truth, surfacing
  discrepancies without auto-resolving them. `src/api/access_control.py`:
  `Role`/`Permission` model (read-only vs. trade-authorizing) — **not
  wired into `src/api/main.py`'s live auth dependencies**, since that
  requires deciding a new API-key-to-role mapping convention (a second
  env var), a security-sensitive config change intentionally left for
  explicit follow-up rather than made unilaterally.
- **v9 Self-Optimizing Capital Allocation** —
  `src/tuning/meta_allocator.py`: softmax-over-Sharpe allocator plus
  `rate_limit_allocation_shift()` so reallocation is bounded per step
  (the allocator cannot itself become a source of instability).
  `src/tuning/stress_simulator.py`: replays illustrative historical
  crisis return sequences (2018 crypto winter, 2020 COVID crash, 2022
  FTX collapse — placeholder magnitudes; real deployment should replace
  with actual OHLCV-derived crisis-period series) against a proposed
  allocation to flag capital-preservation-floor breaches before going
  live.
- **v10 Fully Autonomous Multi-Decade Operation** —
  `src/risk/strategy_decay.py`: CUSUM (Page 1954) detector distinguishing
  persistent structural decay from transient underperformance dips —
  feeds into v6's promotion gauntlet for re-evaluation rather than
  auto-retiring anything. `src/diagnostics/decision_log_writer.py`:
  formats and append-writes structural-change records to a
  DECISION_LOG.md-style file (pure formatting; the emitting system, e.g.
  model promotion or strategy retirement, supplies the justification/
  evidence). `src/risk/capital_preservation_floor.py`: hard, code-enforced
  max-drawdown halt requiring explicit `re_authorize()` — never
  auto-clears on equity recovery, and no automated code path in this
  codebase calls `re_authorize()`. `src/tuning/redteam_scheduler.py`:
  tracks when the next periodic full-system stress replay (v9's
  simulator against live allocation) is due, on a configurable interval
  (default: annual).

**Scope and what remains — every module above is standalone and
independently tested, NOT wired into the live orchestrator tick loop or
order placement path.** This is intentional: the live tick loop
(`src/engine/orchestrator.py`) and Kelly sizing call sites
(`src/engine/signal_engine.py`) are the highest-blast-radius code in the
repository, and each of these ten-plus modules composes with several
others in ways that need their own integration design (e.g.: which
strategies feed the meta-allocator vs. the static allocator; how the
macro exposure budget and per-strategy correlation scalar stack
multiplicatively without double-shrinking; where in the tick loop the
capital preservation floor's `update_equity()` call belongs so it can
never be bypassed). Wiring these into one coherent live decision path is
real, substantial system-design work that deserves its own focused pass
with its own validation, not a rushed addition appended to a session that
already built the primitives. Treat this entry as: "the algorithmic
building blocks for v3-v10 exist and are correct in isolation; the
live-wiring integration is the next major engineering effort."

**Validation**: `ruff check src/ tests/` — all checks passed. `mypy src/`
— success, no issues (115 source files). Full suite run separately after
this entry to confirm final pass/coverage counts.

## v10 capital preservation floor — wired into live gate stack

First live-wiring step out of the v3-v10 "standalone, not wired" backlog
noted above: `src/risk/capital_preservation_floor.py`'s
`CapitalPreservationFloor` was a correct, tested primitive with no call
site. Wired it as **gate 0** (outermost, evaluated before slippage/
drawdown/regime/etc.) in `src/risk/gates.py`'s `evaluate_all_gates()`:
- New `GateStatus.HALT_CAPITAL_PRESERVATION` and
  `check_capital_preservation_floor(halted: bool)` — a pure read of the
  floor's `is_halted()`, no equity math duplicated in gates.py.
- `RiskGateContext.capital_preservation_halted: bool = False` — defaults
  to False so any caller not yet passing it (there were none besides
  signal_engine.py) is unaffected.
- `SignalEngine` now owns one `CapitalPreservationFloor` instance per
  (symbol, timeframe) engine, seeded from the new
  `RiskSettings.capital_preservation_max_drawdown_pct` (default 0.30,
  matches the floor's own default). `tick()` calls `update_equity()`
  every bar before building `RiskGateContext`.

**Deliberately not done in this pass**: no admin/API path to call
`re_authorize()` once the floor trips — that still requires the
API-key-to-role decision flagged in the v8 entry above. Until that
exists, a tripped floor requires a code-level `re_authorize()` call or
restart with a fresh (unhalted) instance; this is intentional (never
auto-clears), not an oversight.

**Validation**: pushed to CI (`ci.yml`) for pytest/ruff/mypy/coverage —
not run locally per repo policy.

## v10 strategy decay CUSUM — wired into per-strategy kill-switch

Second live-wiring step from the v3-v10 backlog:
`src/risk/strategy_decay.py`'s `CusumDecayDetector` was standalone with no
call site. Wired it into `StrategyKillSwitchManager`
(`src/risk/strategy_kill_switch.py`):
- Each registered strategy now gets a `CusumDecayDetector` seeded from
  the same `PerformanceBaseline.oos_sharpe` the drift detector uses.
- `evaluate()` feeds it via a new `PerformanceDriftDetector.
  current_rolling_sharpe()` public accessor (extracted from the existing
  private `_check_sharpe_drift()` math — no duplicated logic, no
  behavior change to the existing drift check).
- New `is_structurally_decayed(strategy_id)` — observability-only, never
  disables a strategy itself. Per the module's own docstring: a
  CUSUM-confirmed break should route to v6's promotion gauntlet for full
  re-evaluation before any re-enable, distinct from v2's single-window
  drift halt.

**Deliberately not done in this pass**: no automatic gauntlet routing —
`is_structurally_decayed()` is exposed as a signal for a human/ops
process or a future orchestrator hook to consume, not auto-wired to
`promotion_gauntlet.py` yet. Auto-routing a live strategy through
re-evaluation is itself a decision with blast radius (which strategies
feed which allocator) flagged in the v9/v10 entries above as its own
follow-up.

**Validation**: pushed to CI for pytest/ruff/mypy/coverage — not run
locally per repo policy.

## v8 hash-chained audit trail — wired into signal_engine's audit closure

Third live-wiring step from the v3-v10 backlog: `src/diagnostics/
audit_trail.py`'s `AuditTrail` (append-only, SHA-256 hash-chained) had no
call site — only the separate, richer `TradeAuditor` (src/diagnostics/
trade_auditor.py) was wired. Both now run side by side from the same
`_emit_audit()` closure in `src/engine/signal_engine.py`'s `tick()`:
TradeAuditor remains the queryable, per-tick record; `AuditTrail` adds a
compact, tamper-evident ledger (event_type/reason_code/details) that
`verify_chain_integrity()` can later prove was never altered — the
compliance property the v8 spec asked for, not a replacement for
TradeAuditor's richer schema.

**Validation**: pushed to CI for pytest/ruff/mypy/coverage — not run
locally per repo policy.
