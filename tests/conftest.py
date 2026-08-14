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
