"""
Tests for src/causal/granger.py.

The detector is the only thing standing between a spurious lead-lag and a
feature that tells the model BTC predicts an alt. What matters is that it
abstains on thin history rather than reporting a result computed from it —
a Granger test on 10 observations will happily return a p-value.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.causal.granger import GrangerCausalityDetector, _as_series


def _lagged_pair(n: int = 200, seed: int = 3):
    """BTC returns, and alt *prices* whose returns follow BTC by one step."""
    rng = np.random.default_rng(seed)
    btc_ret = rng.normal(0.0, 0.01, n)
    alt_ret = np.concatenate(([0.0], 0.9 * btc_ret[:-1])) + rng.normal(0.0, 0.001, n)
    alt_prices = 100.0 * np.exp(np.cumsum(alt_ret))
    return btc_ret, alt_prices


# ---------------------------------------------------------------------------
# _as_series
# ---------------------------------------------------------------------------


def test_as_series_coerces_a_list_to_float64():
    assert _as_series([1, 2, 3]).dtype == np.float64


def test_as_series_recasts_an_int_series():
    assert _as_series(pd.Series([1, 2, 3])).dtype == np.float64


# ---------------------------------------------------------------------------
# update()
# ---------------------------------------------------------------------------


def test_a_genuine_lead_lag_is_detected():
    btc_ret, alt_prices = _lagged_pair()
    detector = GrangerCausalityDetector()

    results = detector.update(btc_ret, {"ETH": alt_prices})

    assert "ETH" in results
    assert results["ETH"].is_causal is True
    assert results["ETH"].min_pvalue < 0.05
    assert results["ETH"].treatment == "BTC"
    assert results["ETH"].outcome == "ETH"


def test_thin_btc_history_abstains_entirely():
    # Below the minimum window the test is not meaningful, so there must be
    # no result at all rather than one derived from too little data.
    btc_ret, alt_prices = _lagged_pair()
    assert GrangerCausalityDetector().update(btc_ret[:10], {"ETH": alt_prices}) == {}


def test_a_thin_alt_is_skipped_without_losing_the_others():
    btc_ret, alt_prices = _lagged_pair()
    detector = GrangerCausalityDetector()

    results = detector.update(btc_ret, {"SHORT": alt_prices[:10], "ETH": alt_prices})

    assert "SHORT" not in results
    assert "ETH" in results


def test_a_hostile_alt_series_does_not_abort_the_sweep():
    # One bad feed must not cost the symbols that did have data.
    btc_ret, alt_prices = _lagged_pair()
    detector = GrangerCausalityDetector()

    results = detector.update(btc_ret, {"BAD": ["not", "numbers"], "ETH": alt_prices})

    assert "BAD" not in results
    assert "ETH" in results


# ---------------------------------------------------------------------------
# accessors
# ---------------------------------------------------------------------------


def test_causal_symbols_lists_only_the_causal_ones():
    btc_ret, alt_prices = _lagged_pair()
    detector = GrangerCausalityDetector()
    detector.update(btc_ret, {"ETH": alt_prices})

    assert detector.causal_symbols == ["ETH"]


def test_feature_vector_is_flat_and_numeric():
    btc_ret, alt_prices = _lagged_pair()
    detector = GrangerCausalityDetector()
    detector.update(btc_ret, {"ETH": alt_prices})

    features = detector.to_feature_vector()

    assert features["granger_btc_to_ETH"] == pytest.approx(1.0)
    assert features["granger_lag_ETH"] >= 1.0
    assert all(isinstance(v, float) for v in features.values())


def test_feature_vector_is_empty_before_any_update():
    assert GrangerCausalityDetector().to_feature_vector() == {}
