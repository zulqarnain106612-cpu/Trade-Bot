"""
MODL-005 — model artifacts round-trip, and MODL-001's manifest is wired.

A model that cannot be reloaded cannot be rolled back to, which means the
rollback plan in `docs/security/INCIDENT_RESPONSE.md` §3 step 10 -- "rebuild
from trusted artifact" -- is not executable. So the round trip is a control,
not a convenience.

The integrity check is the other half. `_verify_manifest` reads the file
exactly once and hands the same bytes to the deserialiser, closing the TOCTOU
window in which anything with write access to the model directory could swap
the file between the hash check and the load. Tests here assert that a
tampered file is refused rather than loaded.

Training a real model is slow, so these tests fit the two classifiers
directly and drive `save()` -- the code path that writes the manifest -- with
a provenance record captured the way `train_direction` captures one.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from xgboost import XGBClassifier

from src.models.provenance import (
    ModelProvenance,
    ProvenanceError,
    manifest_path_for,
    read_provenance,
)
from src.models.trainer import MODEL_DIRECTION, MODEL_META_LABEL, ModelTrainer

SYMBOL = "BTC/USDT"
TIMEFRAME = "15m"
COLUMNS = ["frac_diff", "ofi", "atr_momentum"]


def fitted_model(seed: int = 0) -> XGBClassifier:
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(60, len(COLUMNS)))
    y = (X[:, 0] > 0).astype(int)
    model = XGBClassifier(n_estimators=3, max_depth=2, verbosity=0)
    model.fit(X, y)
    return model


@pytest.fixture
def trainer() -> ModelTrainer:
    return ModelTrainer(symbol=SYMBOL, timeframe=TIMEFRAME)


@pytest.fixture
def trained(trainer: ModelTrainer) -> ModelTrainer:
    """A trainer with provenance captured, as a real training run leaves it."""
    rng = np.random.default_rng(3)
    X = rng.normal(size=(80, len(COLUMNS)))
    y = (X[:, 0] > 0).astype(np.int8)
    for name in (MODEL_DIRECTION, MODEL_META_LABEL):
        trainer._record_provenance(
            name,
            X,
            y,
            COLUMNS,
            {"oos_sharpe": 1.4, "accuracy": 0.58},
            trainer._cpcv_methodology(),
        )
    trainer._direction_columns = list(COLUMNS)
    trainer._meta_columns = list(COLUMNS)
    return trainer


@pytest.fixture
def saved(trained: ModelTrainer, tmp_path: Path) -> tuple[Path, Path]:
    return trained.save(fitted_model(), fitted_model(1), tmp_path, version="v-test")


class TestTheRoundTrip:
    def test_both_models_are_written(self, saved):
        direction_path, meta_path = saved
        assert direction_path.exists()
        assert meta_path.exists()

    def test_a_saved_direction_model_loads(self, saved, tmp_path):
        loaded = ModelTrainer.load_direction(tmp_path, SYMBOL, TIMEFRAME)
        assert loaded.predict_proba(np.zeros((1, len(COLUMNS)))).shape == (1, 2)

    def test_a_saved_meta_model_loads(self, saved, tmp_path):
        loaded = ModelTrainer.load_meta(tmp_path, SYMBOL, TIMEFRAME)
        assert loaded.predict_proba(np.zeros((1, len(COLUMNS)))).shape == (1, 2)

    def test_predictions_survive_the_round_trip_exactly(self, trained, tmp_path):
        model = fitted_model()
        sample = np.random.default_rng(9).normal(size=(5, len(COLUMNS)))
        before = model.predict_proba(sample)
        trained.save(model, fitted_model(1), tmp_path, version="v-test")
        after = ModelTrainer.load_direction(tmp_path, SYMBOL, TIMEFRAME).predict_proba(sample)
        assert np.array_equal(before, after)

    def test_the_feature_columns_travel_with_the_model(self, saved, tmp_path):
        # WHICH columns, not just how many: a model fitted on [a, b] and fed
        # [b, a] produces confident nonsense.
        from src.models.trainer import _FEATURE_COLUMNS_ATTR

        loaded = ModelTrainer.load_direction(tmp_path, SYMBOL, TIMEFRAME)
        assert list(getattr(loaded, _FEATURE_COLUMNS_ATTR, [])) == COLUMNS

    def test_a_missing_model_is_a_clear_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No direction model"):
            ModelTrainer.load_direction(tmp_path, SYMBOL, TIMEFRAME)

    @pytest.mark.parametrize("bad", ["../evil", "a/b", "", "..\\evil"])
    def test_a_path_shaped_timeframe_is_refused(self, tmp_path, bad):
        with pytest.raises(ValueError, match="Invalid timeframe"):
            ModelTrainer.load_direction(tmp_path, SYMBOL, bad)


class TestIntegrity:
    def test_a_tampered_artifact_is_refused(self, saved, tmp_path):
        direction_path, _ = saved
        direction_path.write_bytes(direction_path.read_bytes() + b"tampered")
        with pytest.raises(RuntimeError, match="integrity check FAILED"):
            ModelTrainer.load_direction(tmp_path, SYMBOL, TIMEFRAME)

    def test_a_missing_manifest_is_refused(self, saved, tmp_path):
        direction_path, _ = saved
        manifest_path_for(direction_path).unlink()
        with pytest.raises(RuntimeError, match="manifest missing"):
            ModelTrainer.load_direction(tmp_path, SYMBOL, TIMEFRAME)

    def test_a_manifest_for_a_different_file_is_refused(self, saved, tmp_path):
        direction_path, meta_path = saved
        # Swapping the two manifests is the shape of a real mix-up, and the
        # hash is what catches it.
        manifest_path_for(direction_path).write_text(
            manifest_path_for(meta_path).read_text(encoding="utf-8"), encoding="utf-8"
        )
        with pytest.raises(RuntimeError, match="integrity check FAILED"):
            ModelTrainer.load_direction(tmp_path, SYMBOL, TIMEFRAME)


class TestTheManifestCarriesProvenance:
    def test_the_saved_manifest_has_a_provenance_block(self, saved):
        direction_path, _ = saved
        record = read_provenance(direction_path)
        assert isinstance(record, ModelProvenance)

    def test_it_is_complete(self, saved):
        direction_path, _ = saved
        record = read_provenance(direction_path)
        assert record.is_complete, record.missing_fields()

    def test_the_artifact_hash_matches_the_file(self, saved):
        import hashlib

        direction_path, _ = saved
        record = read_provenance(direction_path)
        assert record.artifact_sha256 == hashlib.sha256(direction_path.read_bytes()).hexdigest()

    def test_it_names_the_model_the_symbol_and_the_timeframe(self, saved):
        direction_path, meta_path = saved
        assert read_provenance(direction_path).model_id.startswith(MODEL_DIRECTION)
        assert read_provenance(meta_path).model_id.startswith(MODEL_META_LABEL)
        assert read_provenance(direction_path).symbol == SYMBOL
        assert read_provenance(direction_path).timeframe == TIMEFRAME

    def test_it_records_the_validation_methodology(self, saved):
        direction_path, _ = saved
        assert "CPCV" in read_provenance(direction_path).validation_methodology

    def test_it_records_the_seed(self, saved, trainer):
        direction_path, _ = saved
        assert read_provenance(direction_path).random_seed == trainer._xgb_cfg.random_state

    def test_the_two_models_have_different_ids(self, saved):
        direction_path, meta_path = saved
        assert read_provenance(direction_path).model_id != read_provenance(meta_path).model_id

    def test_the_legacy_keys_are_still_there(self, saved):
        # A reader written against the old {file, sha256} manifest must not
        # break because provenance was added beside it.
        direction_path, _ = saved
        manifest = json.loads(manifest_path_for(direction_path).read_text(encoding="utf-8"))
        assert manifest["file"] == direction_path.name
        assert len(manifest["sha256"]) == 64


class TestSavingWithoutHavingTrained:
    def test_a_model_saved_without_provenance_still_loads(self, trainer, tmp_path):
        # A rollback, or a hand-built fixture. Writing a record full of
        # blanks would assert provenance that does not exist.
        trainer.save(fitted_model(), fitted_model(1), tmp_path, version="v-rollback")
        assert ModelTrainer.load_direction(tmp_path, SYMBOL, TIMEFRAME) is not None

    def test_its_manifest_has_no_provenance_block(self, trainer, tmp_path):
        direction_path, _ = trainer.save(
            fitted_model(), fitted_model(1), tmp_path, version="v-rollback"
        )
        assert read_provenance(direction_path) is None

    def test_provenance_for_reports_nothing_before_training(self, trainer):
        assert trainer.provenance_for(MODEL_DIRECTION) is None

    def test_provenance_for_returns_a_copy(self, trained):
        first = trained.provenance_for(MODEL_DIRECTION)
        first["model_id"] = "mutated"
        assert trained.provenance_for(MODEL_DIRECTION)["model_id"] != "mutated"


class TestTheCapturedRecord:
    def test_it_hashes_the_training_data(self, trained):
        record = trained.provenance_for(MODEL_DIRECTION)
        assert len(record["training_data_hash"]) == 64

    def test_it_hashes_the_feature_schema(self, trained):
        record = trained.provenance_for(MODEL_DIRECTION)
        assert len(record["feature_schema_hash"]) == 64

    def test_it_carries_the_hyperparameters(self, trained):
        record = trained.provenance_for(MODEL_DIRECTION)
        assert record["hyperparameters"]
        assert "random_state" in record["hyperparameters"]

    def test_it_carries_the_library_versions(self, trained):
        assert "xgboost" in trained.provenance_for(MODEL_DIRECTION)["library_versions"]

    def test_it_has_no_artifact_hash_until_the_artifact_exists(self, trained):
        # The honest shape: a placeholder hash would look like a real one.
        assert "artifact_sha256" not in trained.provenance_for(MODEL_DIRECTION)

    def test_a_truncated_record_is_refused_when_completed(self, trained):
        del trained._provenance[MODEL_DIRECTION]["metrics"]
        with pytest.raises(ProvenanceError, match="metrics"):
            trained._finish_provenance(MODEL_DIRECTION, b"bytes")
