"""Tests for src/upgrade/shadow_deploy.py -- 24h shadow A/B Sharpe gating.

evidently is an optional dependency not installed in CI, so
_generate_evidently_report naturally exercises its ImportError branch
here; a fake module covers the installed-but-failing branch.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import numpy as np

from src.upgrade.shadow_deploy import ModelRecord, ShadowDeployer


def test_model_record_sharpe_below_min_samples_returns_sentinel():
    rec = ModelRecord(name="m", model=None, returns=[0.01, 0.02])
    assert rec.sharpe() == -999.0


def test_model_record_sharpe_zero_std_returns_zero():
    rec = ModelRecord(name="m", model=None, returns=[0.01] * 10)
    assert rec.sharpe() == 0.0


def test_model_record_sharpe_normal_case():
    returns = [0.01, -0.02, 0.03, 0.0, 0.01, -0.01]
    rec = ModelRecord(name="m", model=None, returns=returns)
    arr = np.array(returns)
    expected = float(np.mean(arr) / np.std(arr) * np.sqrt(252))
    assert rec.sharpe() == expected


def test_model_record_age_hours_is_nonnegative_and_small_when_just_started():
    rec = ModelRecord(name="m", model=None)
    assert 0.0 <= rec.age_hours() < 0.01


def _callable_model(x):
    return x * 2


class _PredictModel:
    def predict(self, x):
        return x + 1


def test_call_model_prefers_callable():
    deployer = ShadowDeployer(_callable_model, _callable_model, shadow_hours=0.0)
    assert deployer.predict_incumbent(3) == 6


def test_call_model_falls_back_to_predict_method():
    deployer = ShadowDeployer(_PredictModel(), _PredictModel(), shadow_hours=0.0)
    assert deployer.predict_incumbent(3) == 4


def test_call_model_neither_callable_nor_predict_returns_zero():
    deployer = ShadowDeployer(object(), object(), shadow_hours=0.0)
    assert deployer.predict_incumbent(3) == 0.0


def test_start_resets_timers_and_marks_active():
    deployer = ShadowDeployer(_callable_model, _callable_model, shadow_hours=0.0)
    assert deployer.active is False
    deployer.start()
    assert deployer.active is True


def test_record_return_computes_directional_pnl():
    deployer = ShadowDeployer(_callable_model, _callable_model, shadow_hours=0.0)
    deployer.record_return(actual_return=0.02, incumbent_pred=1.5, challenger_pred=-1.0)
    assert deployer._incumbent.returns == [0.02]
    assert deployer._challenger.returns == [-0.02]


def test_record_return_zero_prediction_yields_zero_direction():
    deployer = ShadowDeployer(_callable_model, _callable_model, shadow_hours=0.0)
    deployer.record_return(actual_return=0.02, incumbent_pred=0.0, challenger_pred=0.0)
    assert deployer._incumbent.returns == [0.0]
    assert deployer._challenger.returns == [0.0]


def test_ready_to_evaluate_false_before_start():
    deployer = ShadowDeployer(_callable_model, _callable_model, shadow_hours=0.0)
    assert deployer.ready_to_evaluate() is False


def test_ready_to_evaluate_true_after_start_with_zero_shadow_hours():
    deployer = ShadowDeployer(_callable_model, _callable_model, shadow_hours=0.0)
    deployer.start()
    assert deployer.ready_to_evaluate() is True


def test_evaluate_returns_none_when_not_ready():
    deployer = ShadowDeployer(_callable_model, _callable_model, shadow_hours=999.0)
    deployer.start()
    assert deployer.evaluate() is None


def _seed_returns(deployer, incumbent_returns, challenger_returns):
    deployer._incumbent.returns = list(incumbent_returns)
    deployer._challenger.returns = list(challenger_returns)


def test_evaluate_promotes_challenger_when_meaningfully_better():
    deployer = ShadowDeployer(_callable_model, _callable_model, shadow_hours=0.0)
    deployer.start()
    _seed_returns(
        deployer,
        incumbent_returns=[0.001, -0.001, 0.001, -0.001, 0.001],
        challenger_returns=[0.02, 0.01, 0.03, 0.02, 0.01],
    )
    with patch.dict(sys.modules, {"evidently": None}):
        result = deployer.evaluate()
    assert result.promoted is True
    assert deployer.active is False
    assert deployer.result is result


def test_evaluate_does_not_promote_when_not_better():
    deployer = ShadowDeployer(_callable_model, _callable_model, shadow_hours=0.0)
    deployer.start()
    _seed_returns(
        deployer,
        incumbent_returns=[0.02, 0.01, 0.03, 0.02, 0.01],
        challenger_returns=[0.001, -0.001, 0.001, -0.001, 0.001],
    )
    with patch.dict(sys.modules, {"evidently": None}):
        result = deployer.evaluate()
    assert result.promoted is False


def test_generate_evidently_report_missing_module_is_silent():
    deployer = ShadowDeployer(_callable_model, _callable_model, shadow_hours=0.0)
    with patch.dict(sys.modules, {"evidently": None}):
        deployer._generate_evidently_report()  # must not raise


def test_generate_evidently_report_success_path():
    deployer = ShadowDeployer(_callable_model, _callable_model, shadow_hours=0.0)
    deployer._incumbent.returns = [0.01, 0.02]
    deployer._challenger.returns = [0.03, 0.04]

    fake_evidently = MagicMock()
    fake_metric_preset = MagicMock()
    fake_report_module = MagicMock()
    fake_report_instance = MagicMock()
    fake_report_module.Report.return_value = fake_report_instance

    with patch.dict(
        sys.modules,
        {
            "evidently": fake_evidently,
            "evidently.metric_preset": fake_metric_preset,
            "evidently.report": fake_report_module,
        },
    ):
        deployer._generate_evidently_report()

    fake_report_instance.run.assert_called_once()
    fake_report_instance.save_html.assert_called_once()


def test_generate_evidently_report_swallows_runtime_failure():
    deployer = ShadowDeployer(_callable_model, _callable_model, shadow_hours=0.0)
    deployer._incumbent.returns = [0.01]
    deployer._challenger.returns = [0.02]

    fake_report_module = MagicMock()
    fake_report_module.Report.side_effect = RuntimeError("boom")

    with patch.dict(
        sys.modules,
        {
            "evidently": MagicMock(),
            "evidently.metric_preset": MagicMock(),
            "evidently.report": fake_report_module,
        },
    ):
        deployer._generate_evidently_report()  # must not raise
