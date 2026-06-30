# Architecture Decision Log

## ADR-001: Triple-barrier labeling + CPCV validation
**Date**: Project inception
**Decision**: Use triple-barrier method (AFML Ch.3) for labels + Combinatorial Purged Cross-Validation (Ch.7)
**Rationale**: Eliminates lookahead bias and serial correlation that breaks standard train/test splits
**Status**: Implemented in src/features/pipeline.py

## ADR-002: Meta-labeling architecture
**Date**: Project inception
**Decision**: Separate XGBoost model for direction (P(long)) and meta-label gate (P(bet))
**Rationale**: Separates "which direction" from "should we bet at all" — reduces false positives
**Status**: Implemented in src/models/trainer.py

## ADR-003: Fractional differencing d=0.4
**Date**: Project inception
**Decision**: Apply fractional diff at d=0.4 to price series
**Rationale**: Achieves stationarity while preserving long memory — López de Prado recommendation
**Status**: Implemented in src/features/pipeline.py

## ADR-004: Half-Kelly with ceiling
**Date**: Project inception
**Decision**: Kelly multiplier=0.5, ceiling=0.25 (25% max position)
**Rationale**: Full Kelly is theoretically optimal but practically causes catastrophic drawdowns; Thorp recommends 0.5×
**Status**: Implemented in src/risk/kelly.py

## ADR-005: SQLite WAL for development
**Date**: Project inception
**Decision**: Use SQLite with WAL mode for all storage
**Rationale**: Zero-dependency, sufficient for single-symbol development and paper trading
**Consequence**: Will need migration to TimescaleDB/QuestDB before multi-symbol live trading
**Status**: Implemented in src/data/storage.py — migration NOT started

## ADR-006: Paper mode as default, live requires explicit unlock
**Date**: Project inception
**Decision**: Default mode=paper; live requires TRADING_MODE=live in .env + all gate passes
**Rationale**: Safety-first — accidental live trading is worse than missed opportunity
**Status**: Implemented in src/execution/ and src/risk/gates.py

## ADR-007: Almgren-Chriss square-root impact model for slippage [GAP-001]
**Date**: 2026-06-23
**Decision**: Model execution cost as spread_bps + impact_coeff_bps * sqrt(qty / adv_20d).
Gate placed first in the gate stack (gate 0) as a pre-trade negative-EV veto,
not folded directly into kelly.py — keeps cost modelling and position sizing
as separately testable concerns.
**Rationale**: Square-root law is the standard TCA model (Almgren & Chriss
2001) and is liquidity-normalised via adv_20d, so the impact coefficient is
comparable across symbols with different absolute volume. Gating on net EV
before sizing means a cost-negative signal never reaches Kelly/risk-gate
logic at all, rather than being sized down after the fact.
**Trade-off accepted**: the gate fails open (passes) when no SlippageEstimate
is supplied, so it has zero protective effect until a call site is wired to
populate it (TASK-009 in OPEN_TASKS.md). This was deliberate — failing closed
would have silently blocked all existing paper-trading callers the moment
this gate was added, which is worse than a known, tracked gap.
**Status**: src/risk/slippage.py implemented and gated in src/risk/gates.py.
Live-path wiring (signal_engine.py / live.py) is open — see TASK-009.
Live trading must remain blocked until that follow-up lands; do not treat
"gate 0 exists" as equivalent to "gate 0 is protecting live trades."

---
## Add new decisions below this line when implementing changes

## ADR-XXX (pending number): Intelligence feature wiring — blocked on API provisioning
**Date**: 2026-07-01
**Decision**: Do NOT fake-train on the 15 intelligence features (GAP-015) using
default/constant values. Wait for real Glassnode (Professional plan) +
CryptoQuant API keys, then build a proper historical backfill before
retraining.
**Rationale**: src/intelligence/client.py's get_exchange_netflow(),
get_whale_activity(), get_funding_rate() and
src/intelligence/providers/binance_provider.py's fetch_metrics() are all
current-snapshot-only — no since/until params anywhere. Neither
GLASSNODE_API_KEY nor CRYPTOQUANT_API_KEY is configured today (confirmed:
empty in .env, glassnode_enabled=False / cryptoquant_enabled=False in
intelligence_aggregator_init logs). Training on 24 features today would
mean ~10 of the 15 new columns are constant/0.0 across all of history —
not a real signal, and a real risk of the model latching onto spurious
correlations with a constant or quietly ignoring the column outright.
Scoped plan for when keys are available (multi-session):
  1. Provision Glassnode Professional plan (historical depth + API access;
     https://studio.glassnode.com/pricing) and a CryptoQuant plan with
     historical endpoint access. Add real values to .env
     (GLASSNODE_API_KEY / CRYPTOQUANT_API_KEY — placeholders already added
     to .env and .env.example this session).
  2. Extend src/intelligence/client.py with historical-range fetch methods
     (Glassnode metric endpoints take `s`/`u` unix-timestamp since/until
     params per https://docs.glassnode.com/basic-api/api — same auth/base
     URL as the existing live methods, additive change, not a rewrite).
  3. Build a backfill script that walks historical OHLCV bar timestamps
     (already in src/data/storage.py) and fetches/stores the matching
     15-feature vector per bar — likely a new src/data/storage.py table
     (intelligence_features_history) plus a one-off backfill CLI script,
     mirroring the migration pattern already in storage.py
     (PRAGMA user_version / _MIGRATIONS).
  4. Extend src/features/pipeline.py's FEATURE_COLUMNS (currently 9) to
     include the 15 intelligence_* columns from
     src/features/intelligence_features.py, sourced from the new history
     table instead of a live fetch (live fetch stays for inference only,
     as already wired in signal_engine.py's TASK-010 block).
  5. Retrain via src/models/trainer.py once the new 24-column feature
     matrix has real historical coverage across the full training window
     (check coverage % per column before accepting — a column that's
     still mostly NaN/default after backfill should be dropped, not kept).
  6. Re-run full CPCV validation (ADR-001) on the 24-feature model before
     promoting it over the current 9-feature model — compare out-of-sample
     Sharpe/drawdown, don't assume more features = better.
**Status**: BLOCKED — awaiting API key provisioning (user action, outside
agent scope: account creation + billing).
