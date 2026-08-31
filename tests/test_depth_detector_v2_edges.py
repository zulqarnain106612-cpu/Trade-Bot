"""Edge-case coverage for src/regime/depth_detector_v2.py.

Complements tests/test_depth_detector_v2.py (which covers the main fit/
predict path) with the error and fallback branches: the no-matching-columns
guard, the empty-slice guard, the non-"full" covariance branches of
_assign_labels, and the except-return-0.0 paths in the ADX/BB helpers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from src.regime.depth_detector_v2 import (
    REGIME_LABELS,
    DepthDetectorV2,
    _compute_adx,
    _compute_bb_width,
)


def test_fit_raises_when_no_feature_columns_match():
    detector = DepthDetectorV2()
    with pytest.raises(ValueError, match="No matching feature columns"):
        detector.fit(pd.DataFrame({"totally_unrelated": [1.0, 2.0]}))


def test_predict_before_fit_returns_neutral_default():
    detector = DepthDetectorV2()
    pred = detector.predict(pd.DataFrame({"adx_14": [20.0]}))
    assert pred.label == "Trending"
    assert pred.confidence == 0.0
    assert len(pred.weight_vector) == 18
    assert pred.raw_state == 0


def test_predict_with_empty_frame_returns_neutral_default():
    detector = DepthDetectorV2()
    # Model and scaler present, but the slice has no rows -> second guard.
    detector._model = MagicMock()
    detector._scaler = MagicMock()
    pred = detector.predict(pd.DataFrame({"adx_14": []}))
    assert pred.confidence == 0.0
    assert pred.label == "Trending"


def test_load_returns_false_when_artifacts_absent(tmp_path: Path):
    detector = DepthDetectorV2()
    assert detector.load(tmp_path) is False


def test_model_stem_is_stable_and_symbol_specific():
    a = DepthDetectorV2(symbol="BTC/USDT", timeframe="1h")
    b = DepthDetectorV2(symbol="BTC/USDT", timeframe="1h")
    c = DepthDetectorV2(symbol="ETH/USDT", timeframe="1h")
    assert a._model_stem() == b._model_stem()
    assert a._model_stem() != c._model_stem()
    assert len(a._model_stem()) == 8


def _fake_hmm(covariance_type: str, covars, means=None) -> MagicMock:
    model = MagicMock()
    model.covariance_type = covariance_type
    model.covars_ = covars
    model.means_ = means if means is not None else np.zeros((9, 3))
    return model


def test_assign_labels_full_covariance_ranks_by_trace():
    detector = DepthDetectorV2()
    # 9 states, ascending trace -> label order must follow REGIME_LABELS
    covars = np.array([np.eye(2) * (i + 1) for i in range(9)])
    labels = detector._assign_labels(_fake_hmm("full", covars), np.zeros((10, 2)))
    assert labels[0] == REGIME_LABELS[0]
    assert labels[8] == REGIME_LABELS[8]


def test_assign_labels_diag_covariance_uses_sum():
    detector = DepthDetectorV2()
    covars = np.array([[float(i + 1), float(i + 1)] for i in range(9)])
    labels = detector._assign_labels(_fake_hmm("diag", covars), np.zeros((10, 2)))
    assert len(labels) == 9
    assert labels[0] == REGIME_LABELS[0]


def test_assign_labels_tied_covariance_falls_back_to_mean_variance():
    detector = DepthDetectorV2()
    means = np.array([[float(i), float(i) * 2.0, 0.0] for i in range(9)])
    labels = detector._assign_labels(_fake_hmm("tied", np.eye(3), means=means), np.zeros((10, 3)))
    assert len(labels) == 9
    assert set(labels.values()) == set(REGIME_LABELS)


def test_compute_adx_returns_zero_for_short_series():
    s = pd.Series([1.0, 2.0])
    assert _compute_adx(s, s, s, period=14) == 0.0


def test_compute_adx_returns_zero_on_exception():
    # Long enough to clear the length guard, but the arithmetic inside raises
    # on non-numeric data -- the except branch must swallow it, not propagate.
    bad = pd.Series(["a"] * 30)
    assert _compute_adx(bad, bad, bad) == 0.0


def test_compute_adx_normal_series_in_range():
    n = 60
    close = pd.Series(np.linspace(100, 130, n))
    high = close + 1.0
    low = close - 1.0
    adx = _compute_adx(high, low, close)
    assert 0.0 <= adx <= 100.0


def test_compute_bb_width_returns_zero_for_short_series():
    assert _compute_bb_width(pd.Series([1.0, 2.0]), period=20) == 0.0


def test_compute_bb_width_returns_zero_on_exception():
    # Clears the length guard, then rolling().mean() raises on non-numeric data.
    assert _compute_bb_width(pd.Series(["a"] * 30)) == 0.0


def test_compute_bb_width_normal_series_is_positive():
    close = pd.Series(np.random.default_rng(0).normal(100, 5, 60))
    assert _compute_bb_width(close) > 0.0


def test_compute_bb_width_flat_series_is_zero():
    close = pd.Series([100.0] * 60)
    assert _compute_bb_width(close) == 0.0
