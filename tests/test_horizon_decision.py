"""Horizon fusion and fail-closed decision contract tests.

Decides:
  - REG-0030 — horizon decisions cannot bypass the single risk-gated order path
"""

from __future__ import annotations

from types import SimpleNamespace

from src.fusion.horizon_decision import fuse_horizon_predictions


def _prediction(
    horizon_id: int,
    *,
    direction: float = 0.03,
    status: str = "PROMOTED",
    calibrated: bool = True,
):
    return SimpleNamespace(
        horizon_id=horizon_id,
        status=status,
        error=None,
        model_id=f"model-{horizon_id}",
        model_version=f"v-{horizon_id}",
        prediction_ts=100.0,
        oos_score=0.7,
        oos_sample_count=100,
        magnitude_mu=direction,
        magnitude_sigma=0.005,
        confidence_calibrated=calibrated,
        uncertainty_calibrated=calibrated,
    )


def test_fusion_aligns_out_of_order_predictions_by_horizon_id() -> None:
    specs = {
        "short": {"id": 0, "seconds": 30},
        "long": {"id": 7, "seconds": 259200},
    }
    decision = fuse_horizon_predictions(
        [_prediction(7, direction=0.04), _prediction(0, direction=0.02)],
        specs,
        now=101.0,
        regime_id=2,
    )
    assert decision.action == "BUY"
    assert set(decision.contributions) == {0, 7}
    assert set(decision.model_versions) == {0, 7}


def test_fusion_abstains_for_untrained_stale_or_uncalibrated_experts() -> None:
    specs = {"short": {"id": 0, "seconds": 30}}
    untrained = _prediction(0, status="UNTRAINED")
    stale = _prediction(0)
    stale.prediction_ts = 1.0
    uncalibrated = _prediction(0, calibrated=False)
    uncalibrated.oos_score = None
    decision = fuse_horizon_predictions(
        [untrained, stale, uncalibrated],
        specs,
        now=100.0,
        max_age_seconds=20,
        regime_id=2,
    )
    assert decision.action == "ABSTAIN"
    assert decision.abstain_reason == "no fresh OOS-validated experts"


def test_fusion_abstains_when_valid_experts_conflict() -> None:
    specs = {"short": {"id": 0, "seconds": 30}, "long": {"id": 1, "seconds": 120}}
    decision = fuse_horizon_predictions(
        [_prediction(0, direction=0.03), _prediction(1, direction=-0.03)],
        specs,
        now=100.5,
        regime_id=2,
    )
    assert decision.action == "ABSTAIN"
    assert decision.conflict
    assert decision.abstain_reason == "expert disagreement"


def test_fusion_serializes_a_complete_decision() -> None:
    specs = {"short": {"id": 0, "seconds": 30}}
    decision = fuse_horizon_predictions(
        [_prediction(0)],
        specs,
        now=101.0,
        regime_id=2,
    )
    rendered = decision.to_dict()
    assert rendered["action"] == "BUY"
    assert rendered["direction"] == 1
    assert rendered["regime_id"] == 2
    assert rendered["model_versions"] == {0: "v-0"}
    assert rendered["freshness_seconds"] == {0: 1.0}


def test_fusion_rejects_each_unusable_forecast_shape() -> None:
    specs = {"short": {"id": 0, "seconds": 30}}
    mutations = (
        {"model_id": None},
        {"model_version": None},
        {"error": "worker failed"},
        {"confidence_calibrated": False},
        {"uncertainty_calibrated": False},
        {"prediction_ts": None},
        {"oos_score": None},
        {"oos_sample_count": 99},
        {"oos_score": float("nan")},
        {"prediction_ts": 101.0},
        {"prediction_ts": 1.0},
        {"oos_score": 0.5},
        {"magnitude_sigma": 0.0},
        {"magnitude_sigma": float("inf")},
        {"magnitude_mu": float("nan")},
        {"magnitude_sigma": 1e-320},
    )
    for changes in mutations:
        prediction = _prediction(0)
        for key, value in changes.items():
            setattr(prediction, key, value)
        decision = fuse_horizon_predictions(
            [prediction],
            specs,
            now=100.0,
            max_age_seconds=20.0,
            regime_id=2,
        )
        assert decision.action == "ABSTAIN", changes
        assert decision.to_dict()["abstain_reason"] == "no fresh OOS-validated experts"


def test_fusion_abstains_when_regime_or_horizon_is_unavailable() -> None:
    prediction = _prediction(0)
    no_regime = fuse_horizon_predictions(
        [prediction],
        {"short": {"id": 0, "seconds": 30}},
        now=100.0,
        regime_id=None,
    )
    no_horizon = fuse_horizon_predictions(
        [prediction],
        {"other": {"id": 1, "seconds": 120}},
        now=100.0,
        regime_id=2,
    )
    assert no_regime.action == no_horizon.action == "ABSTAIN"
