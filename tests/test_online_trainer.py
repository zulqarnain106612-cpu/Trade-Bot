"""
Tests for src/models/online_trainer.py — TASK-008.

Covers: warmup gating, blend math, fail-open on bad input,
accuracy tracking, persistence round-trip, reset.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pytest

from src.models.online_trainer import (
    _ONLINE_WEIGHT,
    _WARMUP_SAMPLES,
    OnlinePrediction,
    OnlineTrainer,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _random_vec(n: int = 7, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(n)


def _warm_up(trainer: OnlineTrainer, n: int = _WARMUP_SAMPLES + 5, n_features: int = 7) -> None:
    """Feed enough samples to both models to pass the warmup threshold."""
    rng = np.random.default_rng(42)
    for _ in range(n):
        vec = rng.standard_normal(n_features)
        label = int(rng.integers(0, 2))
        trainer.learn_direction(vec, label=label)
        trainer.learn_meta(vec, p_long=0.55, label=label)


# ---------------------------------------------------------------------------
# 1. Pre-warmup: online weight is 0, batch values pass through unchanged
# ---------------------------------------------------------------------------


def test_blend_before_warmup_returns_batch_values():
    trainer = OnlineTrainer()
    vec = _random_vec()
    result = trainer.blend(batch_p_long=0.7, batch_p_bet=0.6, feature_vec=vec)
    assert isinstance(result, OnlinePrediction)
    assert result.online_weight == 0.0
    assert result.p_long == pytest.approx(0.7)
    assert result.p_bet == pytest.approx(0.6)


def test_direction_from_batch_p_long_before_warmup():
    trainer = OnlineTrainer()
    vec = _random_vec()
    r_long = trainer.blend(batch_p_long=0.9, batch_p_bet=0.5, feature_vec=vec)
    r_short = trainer.blend(batch_p_long=0.3, batch_p_bet=0.5, feature_vec=vec)
    assert r_long.direction == 1
    assert r_short.direction == 0


# ---------------------------------------------------------------------------
# 2. Post-warmup: online weight is applied
# ---------------------------------------------------------------------------


def test_blend_after_warmup_applies_online_weight():
    trainer = OnlineTrainer()
    _warm_up(trainer)
    vec = _random_vec()
    result = trainer.blend(batch_p_long=0.7, batch_p_bet=0.6, feature_vec=vec)
    assert result.online_weight == pytest.approx(_ONLINE_WEIGHT)
    assert result.online_samples >= _WARMUP_SAMPLES


def test_blend_p_long_bounded():
    trainer = OnlineTrainer()
    _warm_up(trainer)
    vec = _random_vec()
    result = trainer.blend(batch_p_long=0.8, batch_p_bet=0.7, feature_vec=vec)
    assert 0.0 <= result.p_long <= 1.0
    assert 0.0 <= result.p_bet <= 1.0


# ---------------------------------------------------------------------------
# 3. Fail-open: bad feature vector falls back to batch values
# ---------------------------------------------------------------------------


def test_blend_fails_open_on_nan_features():
    trainer = OnlineTrainer()
    _warm_up(trainer)
    bad_vec = np.full(7, np.nan)
    # Should not raise; should return batch values (weight=0 due to error)
    result = trainer.blend(batch_p_long=0.65, batch_p_bet=0.55, feature_vec=bad_vec)
    assert result.p_long == pytest.approx(0.65)
    assert result.p_bet == pytest.approx(0.55)
    assert result.online_weight == 0.0


def test_learn_direction_does_not_raise_on_bad_input():
    trainer = OnlineTrainer()
    # Should not raise — fail-open contract
    trainer.learn_direction(np.array([np.nan, np.inf, 1.0]), label=1)
    trainer.learn_direction(np.array([]), label=0)


def test_learn_direction_with_predicted_records_outcome():
    trainer = OnlineTrainer()
    trainer.learn_direction(_random_vec(), label=1, predicted=1)
    assert trainer._dir_model.n_samples == 1


def test_learn_meta_with_predicted_records_outcome():
    trainer = OnlineTrainer()
    trainer.learn_meta(_random_vec(), p_long=0.6, label=1, predicted=1)
    assert trainer._meta_model.n_samples == 1


def test_learn_meta_does_not_raise_on_bad_input():
    trainer = OnlineTrainer()
    trainer.learn_meta(np.array([np.nan, np.inf, 1.0]), p_long=0.5, label=1)


# ---------------------------------------------------------------------------
# 4. Accuracy tracking
# ---------------------------------------------------------------------------


def test_accuracy_report_none_before_10_samples():
    trainer = OnlineTrainer()
    report = trainer.accuracy_report()
    assert report["dir_rolling_accuracy"] is None
    assert report["meta_rolling_accuracy"] is None


def test_accuracy_report_after_learning():
    trainer = OnlineTrainer()
    _warm_up(trainer, n=_WARMUP_SAMPLES + 20)
    report = trainer.accuracy_report()
    assert report["dir_samples"] >= _WARMUP_SAMPLES
    # Rolling accuracy is set only when predicted kwarg is supplied
    # (None here since _warm_up doesn't pass predicted)
    assert report["dir_rolling_accuracy"] is None  # no predictions recorded

    # Now record some outcomes
    trainer._dir_model.record_outcome(1, 1)
    trainer._dir_model.record_outcome(0, 0)
    trainer._dir_model.record_outcome(1, 0)
    for _ in range(10):
        trainer._dir_model.record_outcome(1, 1)
    report2 = trainer.accuracy_report()
    assert report2["dir_rolling_accuracy"] is not None
    assert 0.0 <= report2["dir_rolling_accuracy"] <= 1.0


# ---------------------------------------------------------------------------
# 5. Persistence round-trip
# ---------------------------------------------------------------------------


def test_save_and_load_preserves_sample_count():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        trainer = OnlineTrainer(model_dir=path)
        _warm_up(trainer)
        n_before = trainer._dir_model.n_samples
        trainer.save()

        trainer2 = OnlineTrainer(model_dir=path)
        assert trainer2._dir_model.n_samples == n_before


def test_load_corrupt_file_resets_to_fresh():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        # Write garbage
        (path / "online_direction.pkl").write_bytes(b"notapickle")
        trainer = OnlineTrainer(model_dir=path)
        # Should have reset to fresh model — 0 samples
        assert trainer._dir_model.n_samples == 0


# ---------------------------------------------------------------------------
# 6. Reset clears state and deletes files
# ---------------------------------------------------------------------------


def test_reset_clears_sample_count():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        trainer = OnlineTrainer(model_dir=path)
        _warm_up(trainer)
        assert trainer._dir_model.n_samples > 0
        trainer.reset()
        assert trainer._dir_model.n_samples == 0


def test_reset_deletes_persisted_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        trainer = OnlineTrainer(model_dir=path)
        _warm_up(trainer)
        trainer.save()
        assert (path / "online_direction.pkl").exists()
        trainer.reset()
        assert not (path / "online_direction.pkl").exists()


def test_reset_without_model_dir_skips_file_cleanup():
    trainer = OnlineTrainer()  # no model_dir configured
    _warm_up(trainer)
    trainer.reset()  # must not raise despite no target directory
    assert trainer._dir_model.n_samples == 0


def test_save_without_model_dir_is_a_noop():
    trainer = OnlineTrainer()  # no model_dir configured
    _warm_up(trainer)
    trainer.save()  # must not raise; nothing to persist to


def test_save_exception_is_caught_and_logged(monkeypatch):
    import src.models.online_trainer as ot_mod

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir)
        trainer = OnlineTrainer(model_dir=path)
        _warm_up(trainer)

        monkeypatch.setattr(
            ot_mod.joblib, "dump", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
        )
        trainer.save()  # must not raise despite joblib.dump failing


# ---------------------------------------------------------------------------
# 7. sample_count increments correctly
# ---------------------------------------------------------------------------


def test_sample_count_increments():
    trainer = OnlineTrainer()
    assert trainer._dir_model.n_samples == 0
    for i in range(5):
        trainer.learn_direction(_random_vec(seed=i), label=i % 2)
    assert trainer._dir_model.n_samples == 5
