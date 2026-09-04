"""Tests for src/causal/dowhy_scm.py -- DoWhy structural causal model wrapper.

dowhy is an optional dependency not installed in CI, so __init__ naturally
takes the ImportError path here. Tests inject a fake `dowhy` module via
sys.modules to also cover the "available" estimation path.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pandas as pd

from src.causal.dowhy_scm import CausalEstimate, DoWhySCM


def test_init_unavailable_when_dowhy_missing():
    with patch.dict(sys.modules, {"dowhy": None}):
        scm = DoWhySCM()
    assert scm._available is False


def test_init_available_when_dowhy_installed():
    with patch.dict(sys.modules, {"dowhy": MagicMock()}):
        scm = DoWhySCM()
    assert scm._available is True


def test_estimate_effect_unavailable_returns_zero_estimate():
    with patch.dict(sys.modules, {"dowhy": None}):
        scm = DoWhySCM()
    result = scm.estimate_effect(pd.DataFrame({"a": [1]}), "a", "b")
    assert result == CausalEstimate(
        treatment="a", outcome="b", ate=0.0, confidence=0.0, method="unavailable"
    )


def test_estimate_effect_empty_dataframe_returns_zero_estimate():
    with patch.dict(sys.modules, {"dowhy": MagicMock()}):
        scm = DoWhySCM()
    result = scm.estimate_effect(pd.DataFrame(), "a", "b")
    assert result.method == "unavailable"


def _fake_dowhy_with_causal_model(estimate_value=1.5, refutation_p_value=0.3):
    fake_module = MagicMock()
    fake_model_instance = MagicMock()
    fake_module.CausalModel.return_value = fake_model_instance

    identified = MagicMock()
    fake_model_instance.identify_effect.return_value = identified

    estimate = MagicMock()
    estimate.value = estimate_value
    fake_model_instance.estimate_effect.return_value = estimate

    refutation = MagicMock()
    refutation.p_value = refutation_p_value
    fake_model_instance.refute_estimate.return_value = refutation

    return fake_module, fake_model_instance


def test_estimate_effect_happy_path():
    fake_module, _fake_model = _fake_dowhy_with_causal_model(
        estimate_value=2.5, refutation_p_value=0.42
    )
    with patch.dict(sys.modules, {"dowhy": fake_module}):
        scm = DoWhySCM()
        result = scm.estimate_effect(
            pd.DataFrame({"liquidations": [1, 2]}), "liquidations", "price"
        )

    assert result.ate == 2.5
    assert result.confidence == 0.42
    assert result.method == "backdoor.linear_regression"


def test_estimate_effect_none_value_defaults_to_zero_ate():
    fake_module, _fake_model = _fake_dowhy_with_causal_model(estimate_value=None)
    with patch.dict(sys.modules, {"dowhy": fake_module}):
        scm = DoWhySCM()
        result = scm.estimate_effect(pd.DataFrame({"a": [1]}), "a", "b")
    assert result.ate == 0.0


def test_estimate_effect_refutation_failure_defaults_confidence():
    fake_module, fake_model = _fake_dowhy_with_causal_model()
    fake_model.refute_estimate.side_effect = RuntimeError("refutation failed")
    with patch.dict(sys.modules, {"dowhy": fake_module}):
        scm = DoWhySCM()
        result = scm.estimate_effect(pd.DataFrame({"a": [1]}), "a", "b")
    assert result.confidence == 0.5


def test_estimate_effect_model_failure_returns_failed_estimate():
    fake_module = MagicMock()
    fake_module.CausalModel.side_effect = RuntimeError("bad graph")
    with patch.dict(sys.modules, {"dowhy": fake_module}):
        scm = DoWhySCM()
        result = scm.estimate_effect(pd.DataFrame({"a": [1]}), "a", "b")
    assert result.method == "failed"
    assert result.ate == 0.0


def test_batch_estimate_calls_estimate_effect_for_each_pair():
    with patch.dict(sys.modules, {"dowhy": None}):
        scm = DoWhySCM()
    data = pd.DataFrame({"a": [1]})
    results = scm.batch_estimate(data, [("a", "b"), ("c", "d")])
    assert [(r.treatment, r.outcome) for r in results] == [("a", "b"), ("c", "d")]


def test_causal_signal_builds_ate_dict():
    with patch.dict(sys.modules, {"dowhy": None}):
        scm = DoWhySCM()
    signal = scm.causal_signal(pd.DataFrame({"a": [1]}))
    assert set(signal) == {
        "ate_liquidations_on_price",
        "ate_whale_flow_on_price",
        "ate_funding_rate_on_price",
        "ate_sentiment_on_price",
    }
    assert all(v == 0.0 for v in signal.values())
