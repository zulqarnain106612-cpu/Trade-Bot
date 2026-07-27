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
