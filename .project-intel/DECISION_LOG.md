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

---
## Add new decisions below this line when implementing changes
