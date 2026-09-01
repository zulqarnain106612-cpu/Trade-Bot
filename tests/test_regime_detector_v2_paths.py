"""Coverage for the Crypto-Box (DepthDetectorV2) paths in src/regime/detector.py.

These branches are gated behind CRYPTO_BOX=true, so nothing here runs in
the default configuration -- each test sets the env var explicitly via
monkeypatch so it cannot leak into the rest of the session.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from src.regime.detector import RegimeDetector


def _detector() -> RegimeDetector:
    return RegimeDetector(symbol="BTC/USDT", timeframe="1h")


def test_predict_current_v2_returns_none_when_crypto_box_disabled(monkeypatch):
    monkeypatch.delenv("CRYPTO_BOX", raising=False)
    assert _detector().predict_current_v2(engine_outputs={}) is None


def test_predict_current_v2_returns_none_for_falsey_env_value(monkeypatch):
    monkeypatch.setenv("CRYPTO_BOX", "0")
    assert _detector().predict_current_v2(engine_outputs={}) is None


def test_predict_current_v2_returns_none_when_v2_model_absent(monkeypatch):
    monkeypatch.setenv("CRYPTO_BOX", "true")
    detector = _detector()
    detector._depth_v2 = None
    assert (
        detector.predict_current_v2(engine_outputs={}, features=pd.DataFrame({"adx_14": [1.0]}))
        is None
    )


def test_predict_current_v2_returns_none_when_v2_unfitted(monkeypatch):
    monkeypatch.setenv("CRYPTO_BOX", "true")
    detector = _detector()
    v2 = MagicMock()
    v2._model = None
    detector._depth_v2 = v2
    result = detector.predict_current_v2(
        engine_outputs={}, features=pd.DataFrame({"adx_14": [1.0]})
    )
    assert result is None


def test_predict_current_v2_returns_label_when_fitted(monkeypatch):
    monkeypatch.setenv("CRYPTO_BOX", "true")
    detector = _detector()
    v2 = MagicMock()
    v2._model = object()
    v2.predict.return_value = MagicMock(label="Capitulation")
    detector._depth_v2 = v2
    result = detector.predict_current_v2(
        engine_outputs={}, features=pd.DataFrame({"adx_14": [1.0]})
    )
    assert result == "Capitulation"


def test_predict_current_v2_builds_features_when_none_given(monkeypatch):
    monkeypatch.setenv("CRYPTO_BOX", "true")
    detector = _detector()
    v2 = MagicMock()
    v2._model = object()
    v2.predict.return_value = MagicMock(label="Trending")
    detector._depth_v2 = v2
    with patch(
        "src.regime.depth_detector_v2.build_v2_features_from_engine_outputs",
        return_value=pd.DataFrame({"adx_14": [1.0]}),
    ) as mock_build:
        result = detector.predict_current_v2(engine_outputs={"E-03": object()})
    mock_build.assert_called_once()
    assert result == "Trending"


def test_predict_current_v2_swallows_internal_failure(monkeypatch):
    monkeypatch.setenv("CRYPTO_BOX", "true")
    detector = _detector()
    v2 = MagicMock()
    v2._model = object()
    v2.predict.side_effect = RuntimeError("predict blew up")
    detector._depth_v2 = v2
    result = detector.predict_current_v2(
        engine_outputs={}, features=pd.DataFrame({"adx_14": [1.0]})
    )
    assert result is None


def test_save_swallows_depth_v2_save_failure(tmp_path: Path, monkeypatch):
    detector = _detector()
    # Minimal fitted state so the primary joblib.dump path succeeds.
    detector._model = MagicMock()
    detector._scaler = MagicMock()
    detector._fitted = True

    failing_v2 = MagicMock()
    failing_v2.save.side_effect = RuntimeError("disk full")
    detector._depth_v2 = failing_v2

    with patch("src.regime.detector.joblib.dump"), patch("src.regime.detector._write_manifest"):
        path = detector.save(tmp_path)

    failing_v2.save.assert_called_once()
    assert isinstance(path, Path)


def test_save_calls_depth_v2_save_when_present(tmp_path: Path):
    detector = _detector()
    detector._model = MagicMock()
    detector._scaler = MagicMock()
    detector._fitted = True
    v2 = MagicMock()
    detector._depth_v2 = v2

    with patch("src.regime.detector.joblib.dump"), patch("src.regime.detector._write_manifest"):
        detector.save(tmp_path)

    v2.save.assert_called_once()
