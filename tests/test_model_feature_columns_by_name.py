"""
Tests for name-based feature selection at inference.

XGBoost is fitted here on a numpy array, so the artifact records only
`n_features_in_` -- a COUNT, no names. Inference reconciled by slicing
`feature_vec.index[:n]`, which is safe only while the trained set is exactly
the leading columns of the inference vector. That held while models trained
on BASE_FEATURE_COLUMNS alone, and stops holding the moment intelligence
columns enter training: inference injects whichever intelligence fields are
FINITE this tick, training selects whichever passed the COVERAGE gate, and
those subsets need not agree.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from src.features.pipeline import BASE_FEATURE_COLUMNS
from src.models.trainer import ModelTrainer, _attach_feature_columns


_ATTR = "_trade_bot_feature_columns"

_FUNDING = "intelligence_binance_funding_rate_pct"
_NETFLOW = "intelligence_exchange_netflow_7d_zscore"


class _FakeModel:
    """Records the matrix it was asked to score. No auto-created attributes."""

    def __init__(self, n_features: int, columns: list[str] | None) -> None:
        self.n_features_in_ = n_features
        self.scored: np.ndarray | None = None
        self.proba = np.array([[0.4, 0.6]])
        if columns is not None:
            setattr(self, _ATTR, list(columns))

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        self.scored = X
        return self.proba


def _model(n_features: int, columns: list[str] | None = None) -> _FakeModel:
    return _FakeModel(n_features, columns)


def _vec(**intelligence) -> pd.Series:
    """Base features in canonical order, then any intelligence columns."""
    data = dict.fromkeys(BASE_FEATURE_COLUMNS, 0.5)
    data.update(intelligence)
    return pd.Series(data)


def _trainer() -> ModelTrainer:
    trainer = object.__new__(ModelTrainer)
    trainer._log = MagicMock()
    return trainer


class TestNamedSelection:
    def test_the_named_columns_are_scored_in_their_recorded_order(self) -> None:
        model = _model(9, [*BASE_FEATURE_COLUMNS, _FUNDING])
        vec = _vec(**{_NETFLOW: 111.0, _FUNDING: 222.0})
        _trainer().predict_direction(model, vec)
        scored = model.scored
        assert scored.shape == (1, 9)
        assert scored[0, -1] == pytest.approx(222.0)

    def test_a_positional_slice_would_have_taken_the_wrong_column(self) -> None:
        """
        The concrete hazard. Inference emits netflow before funding (that is
        INTELLIGENCE_FEATURE_COLUMNS order), so index[:9] ends in netflow --
        landing netflow's value in funding's slot with no shape error.
        """
        vec = _vec(**{_NETFLOW: 111.0, _FUNDING: 222.0})
        positional_tail = list(vec.index[:9])[-1]
        assert positional_tail == _NETFLOW  # what the old path would have used
        model = _model(9, [*BASE_FEATURE_COLUMNS, _FUNDING])
        _trainer().predict_direction(model, vec)
        assert model.scored[0, -1] == pytest.approx(222.0)

    def test_a_column_absent_this_tick_becomes_nan_not_a_borrowed_value(self) -> None:
        """
        NaN is XGBoost's native "missing" and the honest encoding for a
        feature this tick did not have.
        """
        model = _model(9, [*BASE_FEATURE_COLUMNS, _FUNDING])
        _trainer().predict_direction(model, _vec(**{_NETFLOW: 111.0}))
        assert np.isnan(model.scored[0, -1])

    def test_extra_inference_columns_are_ignored(self) -> None:
        model = _model(len(BASE_FEATURE_COLUMNS), list(BASE_FEATURE_COLUMNS))
        _trainer().predict_direction(model, _vec(**{_NETFLOW: 1.0, _FUNDING: 2.0}))
        assert model.scored.shape == (1, len(BASE_FEATURE_COLUMNS))

    def test_the_direction_verdict_still_derives_from_the_probability(self) -> None:
        model = _model(len(BASE_FEATURE_COLUMNS), list(BASE_FEATURE_COLUMNS))
        model.proba = np.array([[0.7, 0.3]])
        direction, p_long = _trainer().predict_direction(model, _vec())
        assert (direction, p_long) == (0, pytest.approx(0.3))


class TestBackwardCompatibility:
    def test_an_artifact_without_names_keeps_the_positional_behaviour(self) -> None:
        """
        Pre-upgrade models were all trained on exactly BASE_FEATURE_COLUMNS,
        so the positional slice is correct for them.
        """
        model = _model(len(BASE_FEATURE_COLUMNS), None)
        _, p_long = _trainer().predict_direction(model, _vec(**{_NETFLOW: 9.0}))
        assert model.scored.shape == (1, len(BASE_FEATURE_COLUMNS))
        assert p_long == pytest.approx(0.6)

    def test_an_empty_recorded_list_is_treated_as_absent(self) -> None:
        model = _model(len(BASE_FEATURE_COLUMNS), [])
        _trainer().predict_direction(model, _vec())
        assert model.scored.shape == (1, len(BASE_FEATURE_COLUMNS))


class TestArtifactRoundTrip:
    def test_the_loader_attaches_the_saved_columns(self) -> None:
        payload = {"model": MagicMock(), "feature_columns": [*BASE_FEATURE_COLUMNS, _FUNDING]}
        model = _attach_feature_columns(payload)
        assert getattr(model, _ATTR) == [*BASE_FEATURE_COLUMNS, _FUNDING]

    def test_a_payload_without_the_key_attaches_an_empty_list(self) -> None:
        model = _attach_feature_columns({"model": MagicMock()})
        assert getattr(model, _ATTR) == []

    def test_the_attached_list_is_a_copy(self) -> None:
        """A mutated caller list must not silently redefine a model's schema."""
        columns = [*BASE_FEATURE_COLUMNS]
        model = _attach_feature_columns({"model": MagicMock(), "feature_columns": columns})
        columns.append("mutated")
        assert "mutated" not in getattr(model, _ATTR)


class TestMetaModel:
    def test_the_saved_list_is_the_base_portion_and_the_two_signals_are_appended(self) -> None:
        """
        The meta model is fitted on base columns plus p_long and confidence;
        predict_meta must re-append exactly those two.
        """
        meta = _model(11, [*BASE_FEATURE_COLUMNS, _FUNDING])
        _trainer().predict_meta(meta, _vec(**{_FUNDING: 222.0}), p_long=0.8)
        scored = meta.scored
        assert scored.shape == (1, 11)
        assert scored[0, -2] == pytest.approx(0.8)
        assert scored[0, -1] == pytest.approx(0.3)  # abs(0.8 - 0.5)

    def test_named_selection_reaches_the_meta_model_too(self) -> None:
        meta = _model(11, [*BASE_FEATURE_COLUMNS, _FUNDING])
        _trainer().predict_meta(meta, _vec(**{_NETFLOW: 111.0, _FUNDING: 222.0}), p_long=0.6)
        assert meta.scored[0, -3] == pytest.approx(222.0)
