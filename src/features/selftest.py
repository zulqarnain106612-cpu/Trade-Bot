"""
Synthetic round-trip selftest of the feature pipeline.

The description of what a feature computed belongs with the feature, not with
the reporter that reads it — so this lives beside `pipeline.py` rather than
in `src/diagnostics/`, where it used to invert the layer order by importing
back up into features (see GOV-022).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import structlog

from src.features.pipeline import FEATURE_COLUMNS, build_feature_matrix

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def run_pipeline_selftest() -> dict[str, Any]:
    """
    Synthetic round-trip test of the full feature pipeline.

    Generates 800 bars of synthetic OHLCV, runs `build_feature_matrix()`,
    and verifies output shape and NaN absence. Fails fast with structured
    error log so CI catches regressions immediately.

    Reference: Aronson (2006) Ch.6 — verify computational integrity.
    """
    result: dict[str, Any] = {"passed": False, "error": None, "n_features": 0, "n_rows": 0}
    try:
        rng = np.random.default_rng(42)
        n = 800
        close = 30000.0 + np.cumsum(rng.standard_normal(n) * 50)
        df = pd.DataFrame(
            {
                "open": close * 0.999,
                "high": close + np.abs(rng.standard_normal(n) * 30),
                "low": close - np.abs(rng.standard_normal(n) * 30),
                "close": close,
                "volume": np.abs(rng.standard_normal(n) * 100 + 500),
            }
        )
        fm = build_feature_matrix(df)
        # if/raise, not assert: `python -O` strips asserts, and a selftest
        # whose checks vanish reports passed=True on a broken pipeline --
        # the one failure mode a selftest must not have.
        if fm.features is None:
            raise ValueError("feature matrix is None")
        if len(fm.features) == 0:
            raise ValueError("feature matrix empty")
        # list(), not the bare tuple: pandas reads a tuple key as one label.
        if fm.features[list(FEATURE_COLUMNS)].isna().any().any():
            raise ValueError("NaN in features")
        result.update(passed=True, n_features=len(FEATURE_COLUMNS), n_rows=len(fm.features))
        log.info("features.selftest_passed", rows=len(fm.features))
    except Exception as exc:
        result["error"] = str(exc)[:300]
        log.error("features.selftest_failed", error=result["error"], exc_info=True)
    return result
