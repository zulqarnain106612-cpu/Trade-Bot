"""Tests for src/engines/e01_statistical.py -- ARIMA + HMM statistical engine.

pmdarima is an optional dependency not installed in CI, so
_arima_predict naturally exercises its except-fallback (naive drift)
branch here; a fake module via sys.modules covers the auto_arima
success path too.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from src.engines.e01_statistical import E01Statistical


def _ohlcv(n: int = 40, start: float = 100.0, step: float = 1.0) -> pd.DataFrame:
    closes = [start + i * step for i in range(n)]
    return pd.DataFrame({"close": closes})


async def test_run_abstains_when_ohlcv_missing():
    engine = E01Statistical()
    out = await engine.run("BTC/USDT", {"spot": 100.0})
    assert out.confidence == 0.0
    assert out.direction == 0
    assert out.metadata["abstain_reason"] == "insufficient_data"


async def test_run_abstains_when_ohlcv_too_short():
    engine = E01Statistical()
    out = await engine.run("BTC/USDT", {"ohlcv": _ohlcv(n=10), "spot": 100.0})
    assert out.metadata["abstain_reason"] == "insufficient_data"


async def test_run_abstains_when_spot_not_positive():
    engine = E01Statistical()
    out = await engine.run("BTC/USDT", {"ohlcv": _ohlcv(), "spot": 0.0})
    assert out.metadata["abstain_reason"] == "insufficient_data"


async def test_run_happy_path_direction_up():
    engine = E01Statistical(horizon_hours=4)
    data = {"ohlcv": _ohlcv(), "spot": 100.0, "regime_probs": [0.2, 0.7, 0.1]}
    with patch.object(E01Statistical, "_arima_predict", return_value=101.0):
        out = await engine.run("BTC/USDT", data)
    assert out.direction == 1
    assert out.confidence == 0.7
    assert out.predicted_price == 101.0
    assert out.horizon_hours == 4


async def test_run_happy_path_direction_down():
    engine = E01Statistical()
    data = {"ohlcv": _ohlcv(), "spot": 100.0}
    with patch.object(E01Statistical, "_arima_predict", return_value=98.0):
        out = await engine.run("BTC/USDT", data)
    assert out.direction == -1


async def test_run_happy_path_direction_neutral_within_band():
    engine = E01Statistical()
    data = {"ohlcv": _ohlcv(), "spot": 100.0}
    with patch.object(E01Statistical, "_arima_predict", return_value=100.0):
        out = await engine.run("BTC/USDT", data)
    assert out.direction == 0


async def test_run_catches_exception_and_abstains():
    engine = E01Statistical()
    data = {"ohlcv": _ohlcv(), "spot": 100.0}
    with patch.object(E01Statistical, "_arima_predict", side_effect=RuntimeError("boom")):
        out = await engine.run("BTC/USDT", data)
    assert out.metadata["abstain_reason"] == "boom"
    assert out.confidence == 0.0


def test_arima_predict_falls_back_to_naive_drift_when_pmdarima_missing():
    df = _ohlcv(n=40, start=100.0, step=1.0)
    with patch.dict(sys.modules, {"pmdarima": None}):
        pred = E01Statistical._arima_predict(df, spot=100.0)
    window = df["close"].values[-20:]
    expected_drift = (window[-1] - window[0]) / len(window)
    assert pred == 100.0 + expected_drift


def test_arima_predict_success_path_with_fake_pmdarima():
    df = _ohlcv(n=40)
    fake_module = MagicMock()
    fake_model = MagicMock()
    fake_module.auto_arima.return_value = fake_model
    fake_model.predict.return_value = (np.array([105.0]), np.array([[100.0, 110.0]]))

    with patch.dict(sys.modules, {"pmdarima": fake_module}):
        pred = E01Statistical._arima_predict(df, spot=100.0)
    assert pred == 105.0


def test_arima_predict_rejects_confidence_interval_wider_than_spot():
    df = _ohlcv(n=40)
    fake_module = MagicMock()
    fake_model = MagicMock()
    fake_module.auto_arima.return_value = fake_model
    # interval width (500) > abs(spot) (100) -> rejected, falls back to drift
    fake_model.predict.return_value = (np.array([105.0]), np.array([[-200.0, 300.0]]))

    with patch.dict(sys.modules, {"pmdarima": fake_module}):
        pred = E01Statistical._arima_predict(df, spot=100.0)
    window = df["close"].values[-20:]
    expected_drift = (window[-1] - window[0]) / len(window)
    assert pred == 100.0 + expected_drift


def test_hmm_confidence_uses_max_regime_prob():
    assert E01Statistical._hmm_confidence({"regime_probs": [0.1, 0.6, 0.3]}) == 0.6


def test_hmm_confidence_defaults_when_missing():
    assert E01Statistical._hmm_confidence({}) == 0.5


def test_hmm_confidence_defaults_when_empty():
    assert E01Statistical._hmm_confidence({"regime_probs": []}) == 0.5
