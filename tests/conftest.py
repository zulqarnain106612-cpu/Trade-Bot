"""Test-suite wide isolation.

`DuckDBStore(path=None)` falls back to the module-level `_DB_PATH`, which
defaults to the repository's real `data/crypto_intel.duckdb`. Without this
the suite writes production rows into a checked-out working tree and
row-count assertions depend on whatever previous runs left behind.

`_DB_PATH` is read from `DUCKDB_PATH` at import time, so this must run
before any test imports `src.data.duckdb_store` — a `tests/conftest.py`
module body is imported first, which is why this is not a fixture.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


_TMP_DB_DIR = Path(tempfile.mkdtemp(prefix="trade-bot-tests-duckdb-"))
os.environ.setdefault("DUCKDB_PATH", str(_TMP_DB_DIR / "crypto_intel.duckdb"))


def settings_double():
    """A `get_settings()` stand-in whose sub-configs are real, not MagicMocks.

    A bare `MagicMock()` hands back a MagicMock for every attribute, so code
    that compares a config value against a number (`lookback_days <= 0`)
    raises TypeError instead of exercising the path under test. Sub-configs
    with real defaults are substituted; everything else stays auto-specced.
    """
    from unittest.mock import MagicMock

    from src.config import StrategyPortfolioSettings

    cfg = MagicMock()
    cfg.strategy_portfolio = StrategyPortfolioSettings()
    return cfg
