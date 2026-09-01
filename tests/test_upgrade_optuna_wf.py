"""Tests for src/upgrade/optuna_wf.py -- walk-forward Optuna study.

optuna is a real, core (non-optional) dependency here, so the happy-path
tests run a genuine tiny study against a temp SQLite file rather than
mocking optuna's internals -- cheaper to trust than a hand-built fake of
trial.suggest_*/should_prune semantics.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.upgrade.optuna_wf import WalkForwardStudy, _sharpe


def test_sharpe_empty_array_returns_zero():
    assert _sharpe(np.array([])) == 0.0


def test_sharpe_constant_returns_zero_std_returns_zero():
    assert _sharpe(np.array([0.01, 0.01, 0.01])) == 0.0


def test_sharpe_normal_case():
    returns = np.array([0.01, -0.005, 0.02, 0.0])
    expected = float(np.mean(returns) / np.std(returns) * np.sqrt(252))
    assert _sharpe(returns) == pytest.approx(expected)


def test_build_folds_splits_into_n_folds():
    study = WalkForwardStudy(
        study_name="t", train_fn=lambda *a: np.array([0.0]), data=list(range(100)), n_folds=4
    )
    folds = study._build_folds()
    assert len(folds) == 4
    x_train, y_train, x_test, y_test = folds[0]
    assert x_train == y_train
    assert x_test == y_test
    assert len(x_train) < 100


def _always_positive_train_fn(params, X_train, y_train, X_test, y_test, trial):
    return np.array([0.01, 0.02, -0.005])


def _always_failing_train_fn(params, X_train, y_train, X_test, y_test, trial):
    raise RuntimeError("model blew up")


def test_run_happy_path_returns_best_params(tmp_path):
    study = WalkForwardStudy(
        study_name="happy",
        train_fn=_always_positive_train_fn,
        data=list(range(20)),
        n_trials=2,
        n_folds=2,
        storage_path=tmp_path / "study.db",
    )
    best = study.run()
    assert set(best) == {"lr", "batch_size", "dropout", "weight_decay", "n_layers", "n_heads"}


def test_run_with_failing_train_fn_still_completes(tmp_path):
    study = WalkForwardStudy(
        study_name="failing",
        train_fn=_always_failing_train_fn,
        data=list(range(10)),
        n_trials=1,
        n_folds=2,
        storage_path=tmp_path / "study2.db",
    )
    best = study.run()  # every fold raises -> fold_sharpes all -999.0, no crash
    assert isinstance(best, dict)


def test_run_returns_defaults_when_optuna_missing(tmp_path):
    study = WalkForwardStudy(
        study_name="no-optuna",
        train_fn=_always_positive_train_fn,
        data=list(range(10)),
        storage_path=tmp_path / "study3.db",
    )
    with patch.dict(sys.modules, {"optuna": None}):
        best = study.run()
    assert best == {
        "lr": 1e-3,
        "batch_size": 64,
        "dropout": 0.1,
        "weight_decay": 1e-4,
        "n_layers": 2,
        "n_heads": 8,
    }


def test_best_params_returns_empty_before_run():
    study = WalkForwardStudy(study_name="x", train_fn=lambda *a: np.array([0.0]), data=[1, 2])
    assert study.best_params() == {}


def test_best_params_returns_study_best_params_after_run(tmp_path):
    study = WalkForwardStudy(
        study_name="bp",
        train_fn=_always_positive_train_fn,
        data=list(range(10)),
        n_trials=1,
        n_folds=2,
        storage_path=tmp_path / "study4.db",
    )
    study.run()
    assert isinstance(study.best_params(), dict)


def test_best_params_exception_returns_empty():
    study = WalkForwardStudy(study_name="x", train_fn=lambda *a: np.array([0.0]), data=[1, 2])
    fake_study = MagicMock()
    type(fake_study).best_params = property(lambda self: (_ for _ in ()).throw(RuntimeError("x")))
    study._study = fake_study
    assert study.best_params() == {}
