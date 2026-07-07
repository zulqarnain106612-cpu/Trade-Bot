"""
On-chain data fetching layer — EXPERIMENTAL, NOT YET IMPLEMENTED.

This package is planned but not yet built. No production code imports from here.
Attempting ``from src.intelligence.onchain import anything`` will raise ImportError
until this package is populated.

Planned scope (pending Glassnode Professional + CryptoQuant API provisioning):
  - Glassnode on-chain metrics: SOPR, NUPL, Puell Multiple, MVRV-Z
  - CryptoQuant exchange reserve, miner outflow, staking unlock risk
  - Historical backfill helpers (see scripts/backfill_intelligence.py and
    src/intelligence/client.py get_*_history() methods — those are the
    live/history fetch layer; this package will be the storage-aligned
    processing layer once keys are provisioned).

Status: GAP-018 — directory added with stub so ImportError is explicit and
  structured, not a silent missing-module crash. Remove this stub and add real
  modules once API keys are provisioned and backfill is validated.
"""
# Nothing exported until implemented.
__all__: list[str] = []
