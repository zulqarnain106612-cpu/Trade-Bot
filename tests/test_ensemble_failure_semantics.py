"""
Tests for how EnsemblePredictor handles members that cannot predict.

A failed member used to be recorded as predicting 0.0 with a flat 0.5
uncertainty. That is not an abstention, it is a vote — and it moved the point
estimate, the disagreement term and the best-model choice, all without
raising.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.intelligence.ensemble_predictor import EnsemblePrediction, EnsemblePredictor


class _Member:
    def __init__(self, value: float | None, rmse: float = 0.1) -> None:
        self._value = value
        self.rmse = rmse
        self.model = object()  # fitted

    @property
    def is_fitted(self) -> bool:
        return self.model is not None

    def predict(self, features):
        if self._value is None:
            raise RuntimeError("member down")
        return self._value

    def predict_with_uncertainty(self, features):
        return self.predict(features), self.rmse

    def get_performance_metrics(self):
        return {"rmse": self.rmse}


def _ensemble(members: dict[str, _Member], weights: dict[str, float]) -> EnsemblePredictor:
    ens = object.__new__(EnsemblePredictor)
    ens.models = members
    ens.weights = weights
    ens._update_weights = lambda: None  # type: ignore[method-assign]
    return ens


_FEATURES = pd.DataFrame({"x": [1.0]})


def test_a_failed_member_does_not_drag_the_estimate() -> None:
    # Three of five down, every survivor saying 0.6. The old behaviour
    # returned 0.3 — halved by failures voting zero at full weight.
    ens = _ensemble(
        {
            "a": _Member(0.6),
            "b": _Member(0.6),
            "c": _Member(None),
            "d": _Member(None),
            "e": _Member(None),
        },
        {"a": 0.2, "b": 0.3, "c": 0.2, "d": 0.15, "e": 0.15},
    )
    result = ens.predict(_FEATURES)
    assert result.point_estimate == pytest.approx(0.6)


def test_failed_members_are_reported() -> None:
    ens = _ensemble(
        {"a": _Member(0.6), "b": _Member(None)},
        {"a": 0.5, "b": 0.5},
    )
    result = ens.predict(_FEATURES)
    assert set(result.failed_models) == {"b"}
    assert result.is_degraded is True


def test_a_healthy_ensemble_is_not_degraded() -> None:
    ens = _ensemble({"a": _Member(0.6), "b": _Member(0.4)}, {"a": 0.5, "b": 0.5})
    result = ens.predict(_FEATURES)
    assert result.failed_models == ()
    assert result.is_degraded is False


def test_failed_members_are_excluded_from_disagreement() -> None:
    # Survivors agreeing exactly must read as zero disagreement, not as
    # spread against a fabricated zero.
    ens = _ensemble(
        {"a": _Member(0.6), "b": _Member(0.6), "c": _Member(None)},
        {"a": 0.4, "b": 0.4, "c": 0.2},
    )
    result = ens.predict(_FEATURES)
    assert result.model_disagreement == pytest.approx(0.0, abs=1e-9)


def test_a_failed_member_cannot_be_the_best_model() -> None:
    # Its flat 0.5 uncertainty could beat a healthy model's real RMSE.
    ens = _ensemble(
        {"good": _Member(0.6, rmse=0.9), "down": _Member(None)},
        {"good": 0.5, "down": 0.5},
    )
    assert ens.predict(_FEATURES).best_model == "good"


def test_total_failure_refuses_rather_than_reporting_confident_zero() -> None:
    # Every member contributing 0.0 made the disagreement zero, so a total
    # failure emerged as the most confident forecast the ensemble can make.
    ens = _ensemble({"a": _Member(None), "b": _Member(None)}, {"a": 0.5, "b": 0.5})
    with pytest.raises(RuntimeError, match="every ensemble member failed"):
        ens.predict(_FEATURES)


def test_a_non_finite_prediction_counts_as_a_failure() -> None:
    # A NaN is a failure that did not raise; blending it poisons the whole
    # estimate rather than removing one vote.
    ens = _ensemble(
        {"a": _Member(0.6), "nan": _Member(float("nan"))},
        {"a": 0.5, "nan": 0.5},
    )
    result = ens.predict(_FEATURES)
    assert result.failed_models == ("nan",)
    assert result.point_estimate == pytest.approx(0.6)


def test_survivors_without_weight_are_equal_weighted_not_zeroed() -> None:
    # Cold start: returning 0.0 because no weight exists yet would itself be
    # a prediction.
    ens = _ensemble({"a": _Member(0.4), "b": _Member(0.8)}, {"a": 0.0, "b": 0.0})
    assert ens.predict(_FEATURES).point_estimate == pytest.approx(0.6)


def test_prediction_carries_no_failures_by_default() -> None:
    # The field is additive; existing constructors must keep working.
    assert (
        EnsemblePrediction(
            point_estimate=0.5,
            credible_lower=0.4,
            credible_upper=0.6,
            model_disagreement=0.01,
            aleatoric_uncertainty=0.01,
            epistemic_uncertainty=0.01,
            best_model="a",
            model_weights={},
            individual_predictions={},
        ).failed_models
        == ()
    )


class _Unfitted:
    """A member that was never fitted — model is None, rmse is inf."""

    model = None
    rmse = float("inf")

    @property
    def is_fitted(self) -> bool:
        return self.model is not None

    def predict(self, features):
        return 0.0  # the placeholder every real member returns when unfitted

    def predict_with_uncertainty(self, features):
        # rmse inf -> substituted with 0.1, which beats most fitted models.
        return 0.0, 0.1 if self.rmse == float("inf") else self.rmse

    def get_performance_metrics(self):
        return {"rmse": self.rmse}


def test_an_unfitted_member_does_not_vote() -> None:
    # Its 0.0 is a placeholder, not a forecast of no move.
    ens = _ensemble(
        {"fitted": _Member(0.6), "never_fitted": _Unfitted()},
        {"fitted": 0.5, "never_fitted": 0.5},
    )
    result = ens.predict(_FEATURES)
    assert result.point_estimate == pytest.approx(0.6)
    assert result.failed_models == ("never_fitted",)


def test_an_unfitted_member_does_not_pollute_disagreement() -> None:
    # _update_weights() zero-weights an unfitted model out of the point
    # estimate, but model_disagreement is an UNWEIGHTED std, so the
    # placeholder still moved the reported uncertainty.
    ens = _ensemble(
        {"a": _Member(0.6), "b": _Member(0.6), "unfit": _Unfitted()},
        {"a": 0.4, "b": 0.4, "unfit": 0.0},
    )
    result = ens.predict(_FEATURES)
    assert result.model_disagreement == pytest.approx(0.0, abs=1e-9)


def test_an_unfitted_member_cannot_be_the_best_model() -> None:
    # rmse=inf is substituted with 0.1, lower than this fitted model's 0.9.
    ens = _ensemble(
        {"fitted": _Member(0.6, rmse=0.9), "unfit": _Unfitted()},
        {"fitted": 0.5, "unfit": 0.5},
    )
    assert ens.predict(_FEATURES).best_model == "fitted"


def test_an_all_unfitted_ensemble_refuses() -> None:
    ens = _ensemble({"a": _Unfitted(), "b": _Unfitted()}, {"a": 0.5, "b": 0.5})
    with pytest.raises(RuntimeError, match="every ensemble member failed"):
        ens.predict(_FEATURES)
