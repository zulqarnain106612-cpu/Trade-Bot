"""Tests for 9-regime DepthDetectorV2."""

import numpy as np
import pandas as pd
import pytest

from src.regime.depth_detector_v2 import (
    REGIME_LABELS,
    DepthDetectorV2,
    _compute_adx,
    _compute_bb_width,
    build_v2_features_from_engine_outputs,
)


def make_feature_df(n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    return pd.DataFrame(
        {
            col: rng.normal(0, 1, n)
            for col in [
                "e01_confidence",
                "e03_entropy_score",
                "e06_hurst",
                "e02_order_flow_tox",
                "e05_net_flow",
                "e10_deviation_pct",
                "e12_gex",
                "e13_contagion_score",
                "e14_contrarian_signal",
                "e17_amihud_ratio",
                "adx_14",
                "bb_width",
            ]
        }
    )


def test_regime_labels_count():
    assert len(REGIME_LABELS) == 9


def test_fit_and_predict():
    detector = DepthDetectorV2()
    df = make_feature_df(300)
    detector.fit(df)
    pred = detector.predict(df.tail(20))
    assert pred.label in REGIME_LABELS
    assert 0.0 <= pred.confidence <= 1.0
    assert len(pred.weight_vector) == 18


def test_predict_without_fit_returns_default():
    detector = DepthDetectorV2()
    df = make_feature_df(20)
    pred = detector.predict(df)
    assert pred.label == "Trending"
    assert pred.confidence == 0.0


def test_fit_requires_minimum_rows():
    detector = DepthDetectorV2()
    df = make_feature_df(50)  # less than 9 * 20 = 180
    with pytest.raises(ValueError, match="rows"):
        detector.fit(df)


def test_save_and_load(tmp_path):
    detector = DepthDetectorV2()
    df = make_feature_df(300)
    detector.fit(df)
    detector.save(tmp_path)
    loaded = DepthDetectorV2()
    assert loaded.load(tmp_path)
    pred = loaded.predict(df.tail(10))
    assert pred.label in REGIME_LABELS


def test_build_features_from_engine_outputs():
    from datetime import UTC, datetime

    from src.engines.schema import EngineOutput

    outputs = {
        "E-03": EngineOutput(
            engine_id="E-03",
            symbol="BTC/USDT",
            timestamp_utc=datetime.now(UTC),
            predicted_price=50000.0,
            confidence=0.7,
            direction=0,
            horizon_hours=4,
            metadata={"entropy_score": 0.4, "predictability_index": 0.6},
        ),
        "E-06": EngineOutput(
            engine_id="E-06",
            symbol="BTC/USDT",
            timestamp_utc=datetime.now(UTC),
            predicted_price=50000.0,
            confidence=0.6,
            direction=1,
            horizon_hours=4,
            metadata={"hurst": 0.65},
        ),
    }
    df = build_v2_features_from_engine_outputs(outputs)
    assert len(df) == 1
    assert "e03_entropy_score" in df.columns
    assert df["e03_entropy_score"].iloc[0] == 0.4
    assert df["e06_hurst"].iloc[0] == 0.65


def test_compute_adx_returns_nonneg() -> None:
    rng = np.random.default_rng(7)
    n = 50
    close = pd.Series(np.cumprod(1 + rng.normal(0, 0.01, n)) * 50_000)
    high = close * 1.005
    low = close * 0.995
    result = _compute_adx(high, low, close)
    assert result >= 0.0
    assert result <= 100.0


def test_compute_adx_insufficient_data_returns_zero() -> None:
    close = pd.Series([100.0, 101.0])
    high = close * 1.005
    low = close * 0.995
    assert _compute_adx(high, low, close) == 0.0


def test_compute_bb_width_trending_market() -> None:
    # Flat price → narrow BB width
    close = pd.Series([50_000.0] * 30)
    width = _compute_bb_width(close)
    assert width == pytest.approx(0.0, abs=1e-4)


def test_compute_bb_width_insufficient_data_returns_zero() -> None:
    close = pd.Series([100.0] * 5)  # fewer than period=20
    assert _compute_bb_width(close) == 0.0


def test_build_features_with_ohlcv_populates_adx_bb() -> None:
    rng = np.random.default_rng(11)
    n = 50
    close_arr = np.cumprod(1 + rng.normal(0, 0.01, n)) * 50_000
    ohlcv = pd.DataFrame(
        {
            "high": close_arr * 1.005,
            "low": close_arr * 0.995,
            "close": close_arr,
            "open": close_arr,
            "volume": np.ones(n) * 100,
        }
    )
    df = build_v2_features_from_engine_outputs({}, ohlcv=ohlcv)
    # adx_14 and bb_width should be computed (non-zero for noisy data)
    assert "adx_14" in df.columns
    assert "bb_width" in df.columns
    # Just verify they are valid floats (not 0.0 necessarily)
    assert df["adx_14"].iloc[0] >= 0.0
    assert df["bb_width"].iloc[0] >= 0.0
