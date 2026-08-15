# v2 Implementation Plan — Multi-Strategy Portfolio Engine

Active version per ROADMAP.md. Sequential sub-tasks; each closes with the
full gate before the next starts.

## Existing assets to build on (already in repo, verified)
- `src/strategies/position_sizing.py` — Carver forecast-scaled sizing,
  vol targeting, correlation-aware sizing, AFML bet-sizing. Pure functions,
  already cited (Carver 2019, López de Prado 2018, Kelly 1956).
- `src/risk/portfolio_correlation.py` (Gap-005) — EWM rolling correlation
  tracker, `correlation_scalar()` already reduces sizing for correlated
  positions. Currently asset-vs-book; needs extension to strategy-vs-book.
- `src/risk/kelly.py`, `src/risk/gates.py` — Kelly ceiling + gate enforcement.
- `src/risk/performance_drift.py` — drift detection primitive, reusable for
  per-strategy kill-switch.
- `src/regime/detector.py` — HMM regime signal, usable as a strategy-fit
  filter for the registry.
- ccxt providers (`src/intelligence/providers/{binance,okx,bybit}_provider.py`)
  — already fetch OHLCV; funding-rate endpoints available via ccxt but not
  yet wired.

## Sub-task 1 — Strategy Registry & Interface Contract
- New `src/strategies/registry.py`: `StrategyBase` protocol with
  `generate_signal(bar) -> Signal(direction, confidence, regime_fit)`,
  `required_capital_fraction()`, `strategy_id`.
- Orchestrator (`src/engine/orchestrator.py`) consumes registry instead of
  hardcoded strategy calls — additive change, existing strategies wrapped
  first so nothing regresses.
- Tests: contract tests in `tests/test_strategy_registry.py` for
  registration, duplicate-id rejection, malformed-signal rejection.

## Sub-task 2 — New Strategy Families (net-new signal, not existing logic)
Implement in `src/strategies/`, each as an isolated pure-function module
matching the registry contract:
1. **Mean-reversion pairs** (`mean_reversion.py`) — cointegration test
   (Engle-Granger) on top-N liquid pairs from existing OHLCV data already
   pulled by providers; z-score entry/exit.
2. **Funding-rate carry** (`funding_carry.py`) — needs funding-rate history;
   ccxt exposes `fetch_funding_rate_history()` on Binance/OKX/Bybit — add a
   thin fetch method to each provider (`src/intelligence/providers/*`)
   mirroring the existing OHLCV fetch pattern.
3. **Breakout / volume-profile** (`breakout.py`) — uses existing OHLCV,
   volume-weighted range breakout with ATR-based stop.
4. **Cross-sectional momentum** (`xsec_momentum.py`) — ranks the traded
   universe by N-day return, long top decile / avoid bottom decile; reuses
   existing feature pipeline in `src/features/`.
Each strategy ships with: unit tests, a walk-forward out-of-sample backtest
script under `scripts/backtest_<name>.py`, and a written note in
`DECISION_LOG.md` on why the signal is expected to be uncorrelated with
existing strategies (avoids Domain Prior violation: "validate signals
out-of-sample").

## Sub-task 3 — Strategy-Level Risk Extension
- Extend `portfolio_correlation.py`: add `StrategyCorrelationTracker` that
  tracks realized-return correlation between strategies (not just assets),
  reusing the existing EWM machinery — do not duplicate, parametrize.
- Extend `kelly.py` sizing call sites to take a strategy-correlation scalar
  alongside the existing asset-correlation scalar (multiplicative, both
  ceilings apply — Kelly remains a ceiling per Domain Prior).
- Per-strategy kill-switch: wire `performance_drift.py`'s existing drift
  detector per-strategy-id; auto-disable (not delete) a strategy whose
  rolling Sharpe crosses below a configured threshold; re-enable requires
  the same out-of-sample gauntlet as initial promotion.

## Sub-task 4 — P&L Attribution
- New `src/diagnostics/attribution.py`: per-strategy realized P&L, Sharpe,
  max drawdown, hit-rate — sourced from existing order/fill records in
  `src/execution/order_manager.py` (tag fills with strategy_id at signal
  origination in Sub-task 1).
- Expose via existing FastAPI layer (`src/api/main.py`) as a read endpoint,
  consistent with v8's future access-control layer (no write/trade action
  added here — read-only per current scope).

## Validation gate before declaring v2 complete
```bash
uv run ruff check --fix src/ && uv run mypy src/ && uv run pytest tests/ -x -q
python3 scripts/check_coverage_floors.py
```
Plus: each new strategy must show out-of-sample walk-forward results with
positive expectancy and correlation < 0.3 with every existing strategy in
the registry (else it doesn't get promoted — dead code is not shipped as a
"strategy"). Run all four new strategies in paper trading for an agreed
evaluation window with zero correctness issues before moving to v3.

## Data/knowledge already available vs. needed
| Need | Status |
|---|---|
| OHLCV historical data | Available now via existing ccxt providers |
| Funding-rate history | Needs one new fetch method per provider (ccxt supports it natively) |
| Cointegration testing | `statsmodels` — check requirements.lock; add via pip per Hard Rules if absent (no `uv add`) |
| Cross-sectional universe list | Derive from existing exchange symbol lists already fetched |
| Walk-forward harness | Does not exist yet — build once in Sub-task 2, reuse for v4/v6 |

No paid data sources required for v2 — consistent with cost policy
(free-only in dev; paid services deferred until paper trading proves
performance).
