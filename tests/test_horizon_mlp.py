"""Horizon model training, durable labels, and challenger lifecycle tests.

Decides:
  - MODL-001 — model training and artifact lifecycle requirements
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

from src.models.horizon_learning import HorizonLearningStore
from src.models.horizon_mlp import HorizonMLPExpert
from src.models.mlp import MLPHead
from src.workers.orchestrator import WorkerTask, _run_horizon_inference


def test_horizon_mlp_trains_persists_and_is_used_by_its_worker(tmp_path: Path) -> None:
    names = ("close_delta", "volume")
    instrument_hash = hashlib.sha256(b"BTC/USDT").hexdigest()[:16]
    expert = HorizonMLPExpert(names, seed=11, horizon_id=3, instrument_hash=instrument_hash)
    assert isinstance(expert.encoder, MLPHead)

    rows = [{"close_delta": float(index) / 10.0, "volume": float(index % 3)} for index in range(20)]
    for row in rows:
        expert.partial_fit(row, matured_return=row["close_delta"] * 0.02 - 0.01)

    model_dir = tmp_path / "horizon-3"
    artifact = model_dir / "mlp.joblib"
    expert.save(artifact)
    from src.models.provenance import read_provenance

    active_artifact = HorizonMLPExpert.active_artifact_path(artifact)
    provenance = read_provenance(active_artifact)
    assert provenance is not None and provenance.is_complete
    assert provenance.feature_schema_hash == expert.schema_hash
    # The first label initializes the expert; each later label is scored
    # against the preceding parameters before the online update.
    assert provenance.metrics["oos_evaluated"] == 19.0
    restored = HorizonMLPExpert.load(artifact, expected_schema_hash=expert.schema_hash)
    assert restored.n_samples == 20

    result = _run_horizon_inference(
        WorkerTask(
            task_id="trained-3",
            horizon_id=3,
            symbol="BTC/USDT",
            features=rows[-1],
            horizon_seconds=900,
            source_event_ts=1_700_000_000.0,
            architecture_names=("tft", "mlp"),
            model_dir=str(model_dir),
        )
    )
    assert result.status == "TRAINED"
    assert result.model_id == f"h3-mlp-{instrument_hash}"
    assert result.training_samples == 20
    assert result.feature_schema_hash == expert.schema_hash
    assert result.model_version
    assert result.magnitude_sigma > 0.0
    assert result.oos_score is not None and 0.0 <= result.oos_score <= 1.0
    assert not result.confidence_calibrated
    assert not result.uncertainty_calibrated


def test_horizon_mlp_rejects_schema_mismatch_and_isolation(tmp_path: Path) -> None:
    left = HorizonMLPExpert(("a", "b"), seed=3)
    right = HorizonMLPExpert(("a", "b"), seed=4)
    left.partial_fit({"a": 1.0, "b": 2.0}, 0.1)
    right.partial_fit({"a": 1.0, "b": 2.0}, -0.1)
    left_path = tmp_path / "left.joblib"
    right_path = tmp_path / "right.joblib"
    left.save(left_path)
    right.save(right_path)

    left_restored = HorizonMLPExpert.load(left_path)
    right_restored = HorizonMLPExpert.load(right_path)
    assert left_restored.n_samples == right_restored.n_samples == 1
    sample = {"a": 1.0, "b": 2.0}
    before = left_restored.predict(sample)
    with torch.no_grad():
        left_restored.regressor.bias.add_(0.5)
    assert math.isclose(left_restored.predict(sample), before + 0.5, abs_tol=1e-6)
    assert before != right_restored.predict(sample)
    try:
        HorizonMLPExpert.load(left_path, expected_schema_hash="wrong")
    except ValueError as exc:
        assert "schema mismatch" in str(exc)
    else:
        raise AssertionError("schema mismatch must reject the artifact")


def test_horizon_label_matures_on_source_time_and_replay_updates_once(tmp_path: Path) -> None:
    store = HorizonLearningStore(tmp_path / "state")
    horizons = [
        {"id": 0, "seconds": 30, "models": ["mlp"]},
        {"id": 1, "seconds": 60, "models": ["mlp"]},
    ]
    first = store.observe_event(
        event_id="bar-100",
        symbol="BTC/USDT",
        event_ts=100,
        close=100,
        features={"close_delta": 0.1, "volume": 4},
        horizons=horizons,
        model_root=tmp_path / "models",
    )
    second = store.observe_event(
        event_id="bar-129",
        symbol="BTC/USDT",
        event_ts=129,
        close=101,
        features={"close_delta": 0.2, "volume": 5},
        horizons=horizons,
        model_root=tmp_path / "models",
    )
    assert first["matured"] == second["matured"] == 0
    assert second["updated"] == 0

    matured = store.observe_event(
        event_id="bar-130",
        symbol="BTC/USDT",
        event_ts=130,
        close=102,
        features={"close_delta": 0.3, "volume": 6},
        horizons=horizons,
        model_root=tmp_path / "models",
    )
    replay = store.observe_event(
        event_id="bar-130",
        symbol="BTC/USDT",
        event_ts=130,
        close=102,
        features={"close_delta": 0.3, "volume": 6},
        horizons=horizons,
        model_root=tmp_path / "models",
    )
    assert matured["matured"] == matured["updated"] == 1
    assert replay["duplicate"] == 1
    first_stats = store.stats("BTC/USDT")[0]
    assert first_stats["last_matured_label_at"] == 130.0
    assert math.isclose(first_stats["last_matured_label"], math.log(1.02))
    next_maturity = store.observe_event(
        event_id="bar-160",
        symbol="BTC/USDT",
        event_ts=160,
        close=103,
        features={"close_delta": 0.4, "volume": 7},
        horizons=horizons,
        model_root=tmp_path / "models",
    )
    old_replay = store.observe_event(
        event_id="bar-100",
        symbol="BTC/USDT",
        event_ts=100,
        close=100,
        features={"close_delta": 0.1, "volume": 4},
        horizons=horizons,
        model_root=tmp_path / "models",
    )
    instrument = hashlib.sha256(b"BTC/USDT").hexdigest()[:16]
    first_horizon = HorizonMLPExpert.load(
        tmp_path / "models" / "h1" / instrument / "candidate" / "mlp.joblib"
    )
    second_horizon = HorizonMLPExpert.load(
        tmp_path / "models" / "h2" / instrument / "candidate" / "mlp.joblib"
    )
    # The first horizon trained once at t=130 and twice more at t=160;
    # the second horizon trained once at its t=160 maturity.
    assert next_maturity["updated"] == 3
    assert first_horizon.n_samples == 3
    assert second_horizon.n_samples == 1
    assert first_horizon.schema_hash == second_horizon.schema_hash
    assert old_replay["duplicate"] == 1


def test_source_cadence_requires_enough_distinct_events(tmp_path: Path) -> None:
    store = HorizonLearningStore(tmp_path / "state")
    for index in range(6):
        store.observe_event(
            event_id=f"bar-{index}",
            symbol="BTC/USDT",
            event_ts=100 + 60 * index,
            close=100 + index,
            features={"price": 100 + index, "volume": 1},
            horizons=[],
            model_root=tmp_path / "models",
        )
    assert store.source_cadence_seconds("BTC/USDT") == 60.0


def test_source_cadence_is_isolated_by_timeframe(tmp_path: Path) -> None:
    store = HorizonLearningStore(tmp_path / "state")
    for index in range(6):
        for timeframe, interval in (("1m", 60), ("15m", 900)):
            store.observe_event(
                event_id=f"{timeframe}-{index}",
                symbol="BTC/USDT",
                event_ts=100 + interval * index,
                close=100 + index,
                features={"price": 100 + index, "volume": 1},
                horizons=[],
                model_root=tmp_path / "models",
                source_timeframe=timeframe,
            )
    assert store.source_cadence_seconds("BTC/USDT", "1m") == 60.0
    assert store.source_cadence_seconds("BTC/USDT", "15m") == 900.0


def test_candidate_evaluation_requires_paired_fresh_oos_evidence(tmp_path: Path) -> None:
    store = HorizonLearningStore(tmp_path / "state")
    horizons = [{"id": 0, "seconds": 30, "models": ["mlp"]}]
    for index in range(5):
        timestamp = 100.0 + index * 60.0
        close = 100.0 + index
        event_id = f"candidate-eval-{index}"
        store.observe_event(
            event_id=event_id,
            symbol="BTC/USDT",
            event_ts=timestamp,
            close=close,
            features={"price": close, "volume": 1.0},
            horizons=horizons,
            model_root=tmp_path / "models",
            defer_training=True,
            source_timeframe="1m",
        )
        store.record_predictions(
            event_id=event_id,
            symbol="BTC/USDT",
            predictions=[
                SimpleNamespace(
                    status="TRAINED",
                    error=None,
                    horizon_id=0,
                    magnitude_mu=0.005,
                    prediction_ts=timestamp,
                    model_version="incumbent-v1",
                    feature_schema_hash=store._features({"price": close, "volume": 1.0})[1],
                    candidate_magnitude_mu=0.01,
                    candidate_model_version="candidate-v1",
                    candidate_schema_hash=store._features({"price": close, "volume": 1.0})[1],
                )
            ],
        )

    insufficient = store.evaluate_candidate(
        symbol="BTC/USDT",
        horizon_id=0,
        source_timeframe="1m",
        candidate_version="candidate-v1",
        minimum_samples=5,
        max_evidence_age_seconds=1000,
        now=500,
    )
    assert not insufficient["eligible"]
    assert insufficient["outcome"] == "CONTINUE_SHADOW"
    assert insufficient["paired_samples"] == 4

    passed = store.evaluate_candidate(
        symbol="BTC/USDT",
        horizon_id=0,
        source_timeframe="1m",
        candidate_version="candidate-v1",
        minimum_samples=4,
        max_evidence_age_seconds=1000,
        now=500,
    )
    assert passed["eligible"]
    assert passed["outcome"] == "PASS"
    assert passed["candidate_rmse"] < passed["incumbent_rmse"]
    assert (
        store.latest_candidate_evaluation(symbol="BTC/USDT", horizon_id=0, source_timeframe="1m")[
            "evaluation_id"
        ]
        == passed["evaluation_id"]
    )


def test_matured_training_can_run_after_event_persistence(tmp_path: Path) -> None:
    store = HorizonLearningStore(tmp_path / "state")
    horizons = [{"id": 0, "seconds": 30, "models": ["mlp"]}]
    for timestamp, close in ((100.0, 100.0), (160.0, 102.0)):
        store.observe_event(
            event_id=f"event-{timestamp}",
            symbol="BTC/USDT",
            event_ts=timestamp,
            close=close,
            features={"price": close, "volume": 1.0},
            horizons=horizons,
            model_root=tmp_path / "models",
            defer_training=True,
        )
    assert store.stats("BTC/USDT")[0]["matured_samples"] == 1
    assert store.stats("BTC/USDT")[0]["training_samples"] == 0
    assert (
        store.train_pending(symbol="BTC/USDT", horizons=horizons, model_root=tmp_path / "models")[
            "updated"
        ]
        == 1
    )
    assert store.stats("BTC/USDT")[0]["training_samples"] == 1
    instrument = hashlib.sha256(b"BTC/USDT").hexdigest()[:16]
    model_dir = tmp_path / "models" / "h1" / instrument
    assert HorizonMLPExpert.artifact_available(model_dir / "candidate" / "mlp.joblib")
    assert not HorizonMLPExpert.artifact_available(model_dir / "mlp.joblib")


def test_subminute_horizon_abstains_when_source_cadence_is_minute_or_unknown() -> None:
    for cadence in (60.0, None):
        result = _run_horizon_inference(
            WorkerTask(
                task_id=f"h30-{cadence}",
                horizon_id=0,
                symbol="BTC/USDT",
                features={"price": 100.0, "volume": 1.0},
                horizon_seconds=30,
                source_event_ts=100.0,
                architecture_names=("mlp",),
                source_cadence_seconds=cadence,
            )
        )
        assert result.status == "UNAVAILABLE_CADENCE"
        assert result.error is not None

    no_timestamp = _run_horizon_inference(
        WorkerTask(
            task_id="h30-no-time",
            horizon_id=0,
            symbol="BTC/USDT",
            features={"price": 100.0, "volume": 1.0},
            horizon_seconds=30,
            architecture_names=("mlp",),
            source_cadence_seconds=10.0,
        )
    )
    assert no_timestamp.status == "UNAVAILABLE_SOURCE_TIME"


def test_forward_prediction_is_joined_to_its_matured_outcome(tmp_path: Path) -> None:
    store = HorizonLearningStore(tmp_path / "state")
    horizons = [{"id": 0, "seconds": 30, "models": ["mlp"]}]
    store.observe_event(
        event_id="bar-a",
        symbol="BTC/USDT",
        event_ts=100,
        close=100,
        features={"return": 0.01},
        horizons=horizons,
        model_root=tmp_path / "models",
    )
    prediction = SimpleNamespace(
        status="TRAINED",
        error=None,
        magnitude_mu=0.02,
        prediction_ts=101.0,
        horizon_id=0,
        model_version="v1",
    )
    assert (
        store.record_predictions(event_id="bar-a", symbol="BTC/USDT", predictions=[prediction]) == 1
    )
    assert (
        store.record_predictions(event_id="bar-a", symbol="BTC/USDT", predictions=[prediction]) == 0
    )
    store.observe_event(
        event_id="bar-b",
        symbol="BTC/USDT",
        event_ts=130,
        close=101,
        features={"return": 0.02},
        horizons=horizons,
        model_root=tmp_path / "models",
    )
    assert store.stats()[0]["oos_samples"] == 1
    assert store.stats()[0]["oos_directional_accuracy"] == 1.0


def test_event_source_payload_and_regime_survive_store_reopen(tmp_path: Path) -> None:
    store = HorizonLearningStore(tmp_path / "state")
    store.observe_event(
        event_id="provider-event-1",
        symbol="BTC/USDT",
        event_ts=100,
        close=100,
        features={"price": 100.0, "volume": 2.0},
        horizons=[{"id": 0, "seconds": 30, "models": ["mlp"]}],
        model_root=tmp_path / "models",
        provider="persisted_market_bars",
        quality="persisted_bar",
        event_payload={"open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0},
        regime_id=2,
    )
    reopened = HorizonLearningStore(tmp_path / "state")
    with reopened._connect() as db:
        event = db.execute(
            "SELECT provider, quality, schema_version, payload_json, regime_id FROM horizon_events"
        ).fetchone()
    assert event[0:3] == ("persisted_market_bars", "persisted_bar", 1)
    assert '"close":100.0' in event[3]
    assert event[4] == 2
    assert reopened.stats("BTC/USDT")[0]["pending_samples"] == 1


def test_shared_horizon_config_loader_uses_explicit_and_default_paths(tmp_path: Path) -> None:
    from src.models.horizon_config import load_horizon_specs

    config = tmp_path / "horizons.yaml"
    config.write_text(
        "horizons:\n"
        "  short: {id: 0, label: 30s, seconds: 30, models: [mlp]}\n"
        "  long: {id: 1, label: 2m, seconds: 120, models: [mlp]}\n",
        encoding="utf-8",
    )
    assert list(load_horizon_specs(config)) == ["short", "long"]
    assert len(load_horizon_specs()) == 10
    assert load_horizon_specs(tmp_path / "missing.yaml") == {}

    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_horizon_specs(empty) == {}
    null_horizons = tmp_path / "null-horizons.yaml"
    null_horizons.write_text("horizons: null\n", encoding="utf-8")
    assert load_horizon_specs(null_horizons) == {}


def test_shared_horizon_config_loader_rejects_invalid_shapes_and_ids(tmp_path: Path) -> None:
    import pytest

    from src.models.horizon_config import load_horizon_specs

    invalid_documents = (
        ("[]\n", "root must be a mapping"),
        ("horizons: []\n", "horizons must be a mapping"),
        ("horizons:\n  short: 1\n", "must be a mapping"),
        (
            "horizons:\n  short: {id: -1, seconds: 30, models: [mlp]}\n",
            "negative or duplicate id",
        ),
        (
            "horizons:\n  short: {id: 0, seconds: 30, models: [mlp]}\n"
            "  long: {id: 0, seconds: 120, models: [mlp]}\n",
            "negative or duplicate id",
        ),
        ("horizons:\n  short: {id: 0, seconds: 0, models: [mlp]}\n", "positive duration"),
        ("horizons:\n  short: {id: 0, seconds: 30, models: []}\n", "at least one model"),
        (
            "horizons:\n  short: {id: 0, seconds: 30, models: [tft]}\n",
            "without a compatible trainer",
        ),
        (
            "horizons:\n  short: {id: 0, seconds: 30, models: [mlp, mlp]}\n",
            "without a compatible trainer",
        ),
    )
    for index, (contents, error) in enumerate(invalid_documents):
        config = tmp_path / f"invalid-{index}.yaml"
        config.write_text(contents, encoding="utf-8")
        with pytest.raises(ValueError, match=error):
            load_horizon_specs(config)


def test_shared_horizon_config_loader_rejects_missing_and_non_integer_fields(
    tmp_path: Path,
) -> None:
    import pytest

    from src.models.horizon_config import load_horizon_specs

    invalid_documents = (
        (
            "missing-id",
            "horizons:\n  short: {seconds: 30, models: [mlp]}\n",
            KeyError,
            "id",
        ),
        (
            "missing-seconds",
            "horizons:\n  short: {id: 0, models: [mlp]}\n",
            KeyError,
            "seconds",
        ),
        (
            "non-integer-id",
            "horizons:\n  short: {id: invalid, seconds: 30, models: [mlp]}\n",
            ValueError,
            "invalid literal",
        ),
        (
            "non-integer-seconds",
            "horizons:\n  short: {id: 0, seconds: invalid, models: [mlp]}\n",
            ValueError,
            "invalid literal",
        ),
    )
    for name, contents, error_type, message in invalid_documents:
        config = tmp_path / f"{name}.yaml"
        config.write_text(contents, encoding="utf-8")
        with pytest.raises(error_type, match=message):
            load_horizon_specs(config)


def test_canonical_horizon_loader_rejects_architecture_without_online_trainer(
    tmp_path: Path, monkeypatch
) -> None:
    import pytest

    import src.intel as intel_module

    config = tmp_path / "horizons.yaml"
    config.write_text(
        "horizons:\n  long:\n    id: 0\n    label: 1D\n    seconds: 86400\n    models: [tft]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(intel_module, "_HORIZONS_CONFIG", config)
    with pytest.raises(ValueError, match="without a compatible trainer"):
        intel_module.load_horizon_specs()


def test_learning_store_rejects_invalid_features_events_and_out_of_order_source(
    tmp_path: Path,
) -> None:
    store = HorizonLearningStore(tmp_path / "state")
    with pytest.raises(ValueError, match="feature snapshot cannot be empty"):
        store.observe_event(
            event_id="empty",
            symbol="BTC/USDT",
            event_ts=1,
            close=100,
            features={},
            horizons=[],
            model_root=tmp_path / "models",
        )
    with pytest.raises(ValueError, match="invalid feature"):
        store.observe_event(
            event_id="nan",
            symbol="BTC/USDT",
            event_ts=1,
            close=100,
            features={"price": float("nan")},
            horizons=[],
            model_root=tmp_path / "models",
        )
    with pytest.raises(ValueError, match="positive close"):
        store.observe_event(
            event_id="bad-close",
            symbol="BTC/USDT",
            event_ts=2,
            close=0,
            features={"price": 1},
            horizons=[],
            model_root=tmp_path / "models",
        )
    store.observe_event(
        event_id="later",
        symbol="BTC/USDT",
        event_ts=10,
        close=100,
        features={"price": 100},
        horizons=[],
        model_root=tmp_path / "models",
    )
    with pytest.raises(ValueError, match="out-of-order"):
        store.observe_event(
            event_id="earlier",
            symbol="BTC/USDT",
            event_ts=9,
            close=100,
            features={"price": 100},
            horizons=[],
            model_root=tmp_path / "models",
        )


def test_prediction_recording_filters_bad_forecasts_and_scores_matured_labels(
    tmp_path: Path,
) -> None:
    store = HorizonLearningStore(tmp_path / "state")
    root = tmp_path / "models"
    horizons = [{"id": 0, "seconds": 30, "models": ["mlp"]}]
    store.observe_event(
        event_id="e1",
        symbol="BTC/USDT",
        event_ts=100,
        close=100,
        features={"price": 100, "volume": 1},
        horizons=horizons,
        model_root=root,
        regime_id=2,
    )
    schema = hashlib.sha256(b"price\0volume").hexdigest()
    bad = [
        SimpleNamespace(
            status="FAILED", error=None, magnitude_mu=0.1, prediction_ts=100, horizon_id=0
        ),
        SimpleNamespace(
            status="TRAINED", error="bad", magnitude_mu=0.1, prediction_ts=100, horizon_id=0
        ),
        SimpleNamespace(
            status="TRAINED", error=None, magnitude_mu=float("nan"), prediction_ts=100, horizon_id=0
        ),
        SimpleNamespace(
            status="TRAINED", error=None, magnitude_mu=0.1, prediction_ts=0, horizon_id=0
        ),
    ]
    assert store.record_predictions(event_id="e1", symbol="BTC/USDT", predictions=bad) == 0
    good = SimpleNamespace(
        status="TRAINED",
        error=None,
        magnitude_mu=0.02,
        prediction_ts=100,
        horizon_id=0,
        model_version="incumbent",
        feature_schema_hash=schema,
        candidate_magnitude_mu=0.03,
        candidate_model_version="candidate",
        candidate_schema_hash=schema,
    )
    assert store.record_predictions(event_id="e1", symbol="BTC/USDT", predictions=[good]) == 1
    assert store.record_predictions(event_id="e1", symbol="BTC/USDT", predictions=[good]) == 0
    store.observe_event(
        event_id="e2",
        symbol="BTC/USDT",
        event_ts=130,
        close=102,
        features={"price": 102, "volume": 1},
        horizons=horizons,
        model_root=root,
        regime_id=2,
    )
    stats = store.stats("BTC/USDT")
    assert stats[0]["oos_samples"] == 1
    assert stats[0]["candidate_oos_samples"] == 1
    assert 2 in stats[0]["oos_by_regime"]


def test_deferred_training_rejects_horizons_without_online_mlp(tmp_path: Path) -> None:
    store = HorizonLearningStore(tmp_path / "state")
    root = tmp_path / "models"
    mlp_horizons = [{"id": 0, "seconds": 10, "models": ["mlp"]}]
    for event_id, ts, close in (("e1", 100, 100), ("e2", 110, 101)):
        store.observe_event(
            event_id=event_id,
            symbol="BTC/USDT",
            event_ts=ts,
            close=close,
            features={"price": close},
            horizons=mlp_horizons,
            model_root=root,
            defer_training=True,
        )
    result = store.train_pending(
        symbol="BTC/USDT",
        horizons=[{"id": 0, "seconds": 10, "models": ["cnn"]}],
        model_root=root,
    )
    assert result == {"updated": 0, "rejected": 1, "attempted": 1}


def test_horizon_mlp_rejects_duplicate_nonfinite_and_mismatched_features(tmp_path: Path) -> None:
    expert = HorizonMLPExpert(("price",), horizon_id=0)
    with pytest.raises(RuntimeError, match="UNTRAINED"):
        expert.predict({"price": 1})
    with pytest.raises(ValueError, match="finite numeric"):
        expert.partial_fit({"price": float("inf")}, 0.1)
    with pytest.raises(ValueError, match="feature names"):
        expert.partial_fit({"volume": 1}, 0.1)
    with pytest.raises(ValueError, match="must be finite"):
        expert.partial_fit({"price": 1}, float("nan"))
    assert expert.partial_fit({"price": 1}, 0.1, sample_id="once")
    assert not expert.partial_fit({"price": 1}, 0.1, sample_id="once")
    assert expert.predictive_sigma >= 0.0
    assert expert.oos_directional_accuracy is None or 0.0 <= expert.oos_directional_accuracy <= 1.0


def test_horizon_activation_pointer_rejects_escape_and_reports_invalid_pointer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "h1" / "instrument" / "mlp.joblib"
    path.parent.mkdir(parents=True)
    pointer = path.with_suffix(path.suffix + ".active.json")
    pointer.write_text('{"format_version":1,"artifact":"../../outside.joblib"}', encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        HorizonMLPExpert.active_artifact_path(path)
    assert not HorizonMLPExpert.artifact_available(path)
    pointer.write_text('{"format_version":2,"artifact":"mlp.joblib"}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid horizon model activation pointer"):
        HorizonMLPExpert.activation_pointer(path)


def test_learning_store_records_inline_and_deferred_training_failures(tmp_path, monkeypatch):
    store = HorizonLearningStore(tmp_path / "state")
    root = tmp_path / "models"
    horizons = [{"id": 0, "seconds": 30, "models": ["mlp"]}]
    store.observe_event(
        event_id="inline-first",
        symbol="BTC/USDT",
        event_ts=100.0,
        close=100.0,
        features={"price": 100.0, "volume": 1.0},
        horizons=horizons,
        model_root=root,
        defer_training=True,
    )

    def fail_update(self, features, matured_return, *, sample_id=None):
        raise RuntimeError("deliberate model update failure")

    monkeypatch.setattr(HorizonMLPExpert, "partial_fit", fail_update)
    inline = store.observe_event(
        event_id="inline-maturity",
        symbol="BTC/USDT",
        event_ts=130.0,
        close=101.0,
        features={"price": 101.0, "volume": 1.0},
        horizons=horizons,
        model_root=root,
        defer_training=False,
    )
    assert inline["matured"] == 1
    assert inline["rejected"] == 1

    deferred_store = HorizonLearningStore(tmp_path / "deferred-state")
    deferred_root = tmp_path / "deferred-models"
    deferred_store.observe_event(
        event_id="deferred-first",
        symbol="BTC/USDT",
        event_ts=100.0,
        close=100.0,
        features={"price": 100.0, "volume": 1.0},
        horizons=horizons,
        model_root=deferred_root,
        defer_training=True,
    )
    deferred_store.observe_event(
        event_id="deferred-maturity",
        symbol="BTC/USDT",
        event_ts=130.0,
        close=101.0,
        features={"price": 101.0, "volume": 1.0},
        horizons=horizons,
        model_root=deferred_root,
        defer_training=True,
    )
    batch = deferred_store.train_pending(
        symbol="BTC/USDT",
        horizons=horizons,
        model_root=deferred_root,
    )
    assert batch == {"updated": 0, "rejected": 1, "attempted": 1}
    assert "deliberate model update failure" in deferred_store.stats("BTC/USDT")[0]["last_error"]


def test_candidate_evaluation_rejects_invalid_limits_and_empty_evidence(tmp_path):
    store = HorizonLearningStore(tmp_path / "state")
    with pytest.raises(ValueError, match="limits must be positive"):
        store.evaluate_candidate(
            symbol="BTC/USDT",
            horizon_id=0,
            source_timeframe="1m",
            minimum_samples=0,
        )
    empty = store.evaluate_candidate(
        symbol="BTC/USDT",
        horizon_id=0,
        source_timeframe="1m",
        minimum_samples=1,
        now=100.0,
    )
    assert not empty["eligible"]
    assert empty["paired_samples"] == 0


def test_horizon_mlp_rejects_empty_and_duplicate_feature_names():
    with pytest.raises(ValueError, match="non-empty unique"):
        HorizonMLPExpert(())
    with pytest.raises(ValueError, match="non-empty unique"):
        HorizonMLPExpert(("price", "price"))


def test_horizon_mlp_reuses_an_existing_content_addressed_version(tmp_path):
    expert = HorizonMLPExpert(("price",), seed=17, horizon_id=0)
    expert.partial_fit({"price": 100.0}, 0.01, sample_id="same-sample")
    artifact = tmp_path / "models" / "mlp.joblib"
    expert.save(artifact)
    expert.save(artifact)
    restored = HorizonMLPExpert.load(artifact)
    assert restored.n_samples == 1
    assert restored.artifact_sha256 == expert.artifact_sha256


def test_horizon_mlp_load_rejects_corrupt_provenance_and_metadata(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import src.models.horizon_mlp as mlp_module
    import src.models.provenance as provenance

    expert = HorizonMLPExpert(("price",), seed=19, horizon_id=2, instrument_hash="instrument")
    expert.partial_fit({"price": 100.0}, 0.02, sample_id="sample")
    path = tmp_path / "models" / "mlp.joblib"
    expert.save(path)
    artifact_path = HorizonMLPExpert.active_artifact_path(path)
    original_payload = mlp_module.joblib.load(artifact_path)
    manifest = provenance.read_manifest(artifact_path)
    real_hash = provenance.hash_artifact
    real_read_provenance = provenance.read_provenance
    real_joblib_load = mlp_module.joblib.load

    with monkeypatch.context() as patch:
        patch.setattr(provenance, "hash_artifact", lambda payload: "incorrect")
        with pytest.raises(ValueError, match="checksum mismatch"):
            HorizonMLPExpert.load(path)

    with monkeypatch.context() as patch:
        patch.setattr(provenance, "read_provenance", lambda artifact: None)
        with pytest.raises(ValueError, match="provenance is incomplete"):
            HorizonMLPExpert.load(path)

    with monkeypatch.context() as patch:
        patch.setattr(
            provenance,
            "read_provenance",
            lambda artifact: SimpleNamespace(
                is_complete=True,
                artifact_sha256="incorrect",
                feature_schema_hash=expert.schema_hash,
            ),
        )
        with pytest.raises(ValueError, match="provenance checksum mismatch"):
            HorizonMLPExpert.load(path)

    with monkeypatch.context() as patch:
        patch.setattr(
            mlp_module.joblib,
            "load",
            lambda artifact: {**original_payload, "format_version": 2},
        )
        with pytest.raises(ValueError, match="unsupported horizon model artifact"):
            HorizonMLPExpert.load(path)

    with pytest.raises(ValueError, match="different horizon"):
        HorizonMLPExpert.load(path, expected_horizon_id=99)
    with pytest.raises(ValueError, match="different instrument"):
        HorizonMLPExpert.load(path, expected_instrument_hash="different")

    with monkeypatch.context() as patch:
        patch.setattr(
            mlp_module.joblib,
            "load",
            lambda artifact: {**original_payload, "schema_hash": "incorrect"},
        )
        with pytest.raises(ValueError, match="invalid feature schema hash"):
            HorizonMLPExpert.load(path)

    with monkeypatch.context() as patch:
        patch.setattr(
            provenance,
            "read_provenance",
            lambda artifact: SimpleNamespace(
                is_complete=True,
                artifact_sha256=manifest["sha256"],
                feature_schema_hash="incorrect",
            ),
        )
        with pytest.raises(ValueError, match="provenance feature schema mismatch"):
            HorizonMLPExpert.load(path)

    with pytest.raises(ValueError, match="feature schema mismatch"):
        HorizonMLPExpert.load(path, expected_schema_hash="incorrect")

    with monkeypatch.context() as patch:
        patch.setattr(
            mlp_module.joblib,
            "load",
            lambda artifact: {**original_payload, "n_samples": 0},
        )
        with pytest.raises(ValueError, match="no training samples"):
            HorizonMLPExpert.load(path)

    assert (
        real_hash is not None and real_read_provenance is not None and real_joblib_load is not None
    )


def test_horizon_mlp_active_pointer_rejects_missing_versioned_artifact(tmp_path):
    path = tmp_path / "instrument" / "mlp.joblib"
    path.parent.mkdir(parents=True)
    versions = path.parent / f".{path.name}.versions"
    versions.mkdir()
    pointer = path.with_suffix(path.suffix + ".active.json")
    pointer.write_text(
        '{"format_version":1,"artifact":".mlp.joblib.versions/missing.joblib"}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="active horizon model artifact is missing"):
        HorizonMLPExpert.active_artifact_path(path)
    assert not HorizonMLPExpert.artifact_available(path)


def test_horizon_learning_store_migrates_legacy_event_and_sample_tables(tmp_path):
    import sqlite3

    root = tmp_path / "legacy"
    root.mkdir()
    database = root / "horizon_learning.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute(
            """CREATE TABLE horizon_samples (
                sample_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                horizon_id INTEGER NOT NULL,
                event_ts REAL NOT NULL,
                due_ts REAL NOT NULL,
                entry_price REAL NOT NULL,
                features_json TEXT NOT NULL,
                schema_hash TEXT NOT NULL,
                outcome REAL,
                matured_at REAL,
                trained INTEGER NOT NULL DEFAULT 0,
                error TEXT
            )"""
        )
        db.execute(
            """CREATE TABLE horizon_events (
                event_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                event_ts REAL NOT NULL,
                receive_ts REAL NOT NULL,
                close REAL NOT NULL
            )"""
        )

    store = HorizonLearningStore(root)
    with store._connect() as db:
        sample_columns = {row[1] for row in db.execute("PRAGMA table_info(horizon_samples)")}
        event_columns = {row[1] for row in db.execute("PRAGMA table_info(horizon_events)")}
    assert "trained_at" in sample_columns
    assert "candidate_prediction_return" in sample_columns
    assert "provider" in event_columns
    assert "source_timeframe" in event_columns


def test_horizon_event_replay_is_idempotent(tmp_path):
    store = HorizonLearningStore(tmp_path / "state")
    args = {
        "event_id": "same-source-event",
        "symbol": "BTC/USDT",
        "event_ts": 100.0,
        "close": 100.0,
        "features": {"price": 100.0, "volume": 1.0},
        "horizons": [{"id": 0, "seconds": 60, "models": ["mlp"]}],
        "model_root": tmp_path / "models",
        "defer_training": True,
    }
    first = store.observe_event(**args)
    duplicate = store.observe_event(**args)
    assert first["duplicate"] == 0
    assert duplicate["duplicate"] == 1


def test_horizon_event_insert_race_is_reported_as_duplicate(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    store = HorizonLearningStore(tmp_path / "state")
    real_connect = store._connect

    @contextmanager
    def competing_insert():
        with real_connect() as connection:

            class ConnectionProxy:
                def execute(self, sql, parameters=()):
                    if "INSERT OR IGNORE INTO horizon_events" in sql:
                        return SimpleNamespace(rowcount=0)
                    return connection.execute(sql, parameters)

            yield ConnectionProxy()

    monkeypatch.setattr(store, "_connect", competing_insert)
    result = store.observe_event(
        event_id="concurrent-source-event",
        symbol="BTC/USDT",
        event_ts=100.0,
        close=100.0,
        features={"price": 100.0, "volume": 1.0},
        horizons=[],
        model_root=tmp_path / "models",
        defer_training=True,
    )
    assert result == {"duplicate": 1, "matured": 0, "updated": 0, "rejected": 0}


def test_horizon_learning_trains_inline_and_in_batches(tmp_path):
    horizons = [{"id": 0, "seconds": 30, "models": ["mlp"]}]
    inline_store = HorizonLearningStore(tmp_path / "inline-state")
    inline_root = tmp_path / "inline-models"
    inline_store.observe_event(
        event_id="inline-start",
        symbol="BTC/USDT",
        event_ts=100.0,
        close=100.0,
        features={"price": 100.0, "volume": 1.0},
        horizons=horizons,
        model_root=inline_root,
        defer_training=True,
    )
    inline_store.observe_event(
        event_id="inline-label",
        symbol="BTC/USDT",
        event_ts=130.0,
        close=101.0,
        features={"price": 101.0, "volume": 1.0},
        horizons=horizons,
        model_root=inline_root,
        defer_training=True,
    )
    inline = inline_store.observe_event(
        event_id="inline-retrain",
        symbol="BTC/USDT",
        event_ts=160.0,
        close=102.0,
        features={"price": 102.0, "volume": 1.0},
        horizons=horizons,
        model_root=inline_root,
        defer_training=False,
    )
    assert inline["matured"] == 1
    assert inline["updated"] == 2
    assert inline["rejected"] == 0

    batch_store = HorizonLearningStore(tmp_path / "batch-state")
    batch_root = tmp_path / "batch-models"
    for event_id, timestamp, close in (
        ("batch-start", 100.0, 100.0),
        ("batch-label", 130.0, 101.0),
    ):
        batch_store.observe_event(
            event_id=event_id,
            symbol="BTC/USDT",
            event_ts=timestamp,
            close=close,
            features={"price": close, "volume": 1.0},
            horizons=horizons,
            model_root=batch_root,
            defer_training=True,
        )
    batch = batch_store.train_pending(
        symbol="BTC/USDT",
        horizons=horizons,
        model_root=batch_root,
        shadow_minimum_training_samples=1,
    )
    instrument = hashlib.sha256(b"BTC/USDT").hexdigest()[:16]
    candidate = batch_root / "h1" / instrument / "candidate" / "mlp.joblib"
    shadow = batch_root / "h1" / instrument / "shadow" / "mlp.joblib"
    assert batch == {"updated": 1, "rejected": 0, "attempted": 1}
    assert HorizonMLPExpert.artifact_available(candidate)
    assert HorizonMLPExpert.artifact_available(shadow)

    for event_id, timestamp, close in (
        ("batch-second-start", 160.0, 102.0),
        ("batch-second-label", 190.0, 103.0),
    ):
        batch_store.observe_event(
            event_id=event_id,
            symbol="BTC/USDT",
            event_ts=timestamp,
            close=close,
            features={"price": close, "volume": 1.0},
            horizons=horizons,
            model_root=batch_root,
            defer_training=True,
        )
    second_batch = batch_store.train_pending(
        symbol="BTC/USDT",
        horizons=horizons,
        model_root=batch_root,
        shadow_minimum_training_samples=1,
    )
    # Both the event at t=130 and the event at t=160 mature by t=190.
    assert second_batch == {"updated": 2, "rejected": 0, "attempted": 2}


def test_horizon_learning_skips_training_for_non_mlp_horizons(tmp_path):
    store = HorizonLearningStore(tmp_path / "state")
    root = tmp_path / "models"
    horizons = [{"id": 0, "seconds": 30, "models": ["xgboost"]}]
    store.observe_event(
        event_id="non-mlp-start",
        symbol="BTC/USDT",
        event_ts=100.0,
        close=100.0,
        features={"price": 100.0, "volume": 1.0},
        horizons=horizons,
        model_root=root,
        defer_training=True,
    )
    result = store.observe_event(
        event_id="non-mlp-label",
        symbol="BTC/USDT",
        event_ts=130.0,
        close=101.0,
        features={"price": 101.0, "volume": 1.0},
        horizons=horizons,
        model_root=root,
        defer_training=False,
    )
    assert result["matured"] == 1
    assert result["updated"] == 0
    assert result["rejected"] == 0


def _insert_candidate_evidence(store, rows):
    with store._connect() as db:
        for index, overrides in enumerate(rows):
            record = {
                "schema_hash": "schema-a",
                "candidate_schema_hash": "schema-a",
                "candidate_model_version": "candidate-v1",
                "prediction_model_version": "incumbent-v1",
                "outcome": 0.1,
                "matured_at": 999.0,
                "oos_correct": 1,
                "prediction_return": 0.2,
                "candidate_prediction_return": 0.15,
                "candidate_oos_correct": 1,
                "candidate_oos_squared_error": 0.0025,
            }
            record.update(overrides)
            db.execute(
                """INSERT INTO horizon_samples (
                    sample_id, event_id, symbol, horizon_id, event_ts, due_ts,
                    entry_price, features_json, schema_hash, source_timeframe,
                    outcome, matured_at, trained, oos_correct, prediction_return,
                    candidate_prediction_return, candidate_oos_correct,
                    candidate_oos_squared_error, candidate_model_version,
                    prediction_model_version, candidate_schema_hash,
                    prediction_ts, candidate_prediction_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"evidence-{index}",
                    f"event-{index}",
                    "BTC/USDT",
                    0,
                    800.0 + index,
                    900.0 + index,
                    100.0,
                    "{}",
                    record["schema_hash"],
                    "1m",
                    record["outcome"],
                    record["matured_at"],
                    record["oos_correct"],
                    record["prediction_return"],
                    record["candidate_prediction_return"],
                    record["candidate_oos_correct"],
                    record["candidate_oos_squared_error"],
                    record["candidate_model_version"],
                    record["prediction_model_version"],
                    record["candidate_schema_hash"],
                    900.0 + index,
                    900.0 + index,
                ),
            )


def test_candidate_evaluation_rejects_schema_and_skill_regressions(tmp_path):
    schema_store = HorizonLearningStore(tmp_path / "schema-state")
    _insert_candidate_evidence(
        schema_store,
        [
            {"schema_hash": "schema-a", "candidate_schema_hash": "schema-a"},
            {"schema_hash": "schema-b", "candidate_schema_hash": "schema-b"},
        ],
    )
    schema_result = schema_store.evaluate_candidate(
        symbol="BTC/USDT",
        horizon_id=0,
        source_timeframe="1m",
        candidate_version="candidate-v1",
        incumbent_version="incumbent-v1",
        minimum_samples=1,
        max_evidence_age_seconds=100.0,
        now=1000.0,
    )
    assert schema_result["reason"] == "inconsistent feature schemas in paired evidence"

    same_store = HorizonLearningStore(tmp_path / "same-state")
    _insert_candidate_evidence(
        same_store,
        [{"candidate_model_version": "same", "prediction_model_version": "same"}],
    )
    same_result = same_store.evaluate_candidate(
        symbol="BTC/USDT",
        horizon_id=0,
        source_timeframe="1m",
        candidate_version="same",
        incumbent_version="same",
        minimum_samples=1,
        max_evidence_age_seconds=100.0,
        now=1000.0,
    )
    assert same_result["reason"] == "candidate and incumbent artifacts are identical"

    incomplete_store = HorizonLearningStore(tmp_path / "incomplete-state")
    _insert_candidate_evidence(
        incomplete_store,
        [{"candidate_oos_squared_error": None}],
    )
    incomplete = incomplete_store.evaluate_candidate(
        symbol="BTC/USDT",
        horizon_id=0,
        source_timeframe="1m",
        candidate_version="candidate-v1",
        incumbent_version="incumbent-v1",
        minimum_samples=1,
        max_evidence_age_seconds=100.0,
        now=1000.0,
    )
    assert incomplete["reason"] == "paired metrics are incomplete"

    regressed_store = HorizonLearningStore(tmp_path / "regressed-state")
    _insert_candidate_evidence(
        regressed_store,
        [{"candidate_oos_correct": 0, "candidate_oos_squared_error": 0.09}],
    )
    regressed = regressed_store.evaluate_candidate(
        symbol="BTC/USDT",
        horizon_id=0,
        source_timeframe="1m",
        candidate_version="candidate-v1",
        incumbent_version="incumbent-v1",
        minimum_samples=1,
        max_evidence_age_seconds=100.0,
        now=1000.0,
    )
    assert regressed["reason"] == "candidate did not meet both incumbent skill metrics"


def test_horizon_mlp_atomic_pointer_write_cleans_up_after_replace_failure(tmp_path, monkeypatch):
    import src.models.horizon_mlp as mlp_module

    pointer = tmp_path / "models" / "mlp.joblib.active.json"
    pointer.parent.mkdir(parents=True)

    def fail_replace(source, destination):
        raise OSError("atomic replacement failed")

    monkeypatch.setattr(mlp_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="atomic replacement failed"):
        HorizonMLPExpert.write_activation_pointer(
            pointer,
            {"artifact": "model.joblib", "sha256": "abc", "format_version": 1},
        )
    assert not pointer.exists()
    assert list(pointer.parent.iterdir()) == []


def test_horizon_mlp_save_cleans_temporary_version_directory_on_race(tmp_path, monkeypatch):
    import os

    import src.models.horizon_mlp as mlp_module

    expert = HorizonMLPExpert(("price",), seed=23, horizon_id=0)
    expert.partial_fit({"price": 100.0}, 0.01, sample_id="race-sample")
    path = tmp_path / "models" / "mlp.joblib"
    original_replace = os.replace

    def fail_directory_replace(source, destination):
        if Path(source).is_dir() and Path(source).name.startswith(".candidate-"):
            raise FileExistsError("simulated version-directory race")
        return original_replace(source, destination)

    monkeypatch.setattr(mlp_module.os, "replace", fail_directory_replace)
    with pytest.raises(FileExistsError, match="version-directory race"):
        expert.save(path)
    versions = path.parent / f".{path.name}.versions"
    assert not list(versions.glob(".candidate-*"))


def test_horizon_mlp_rejects_unsupported_active_pointer_version(tmp_path):
    path = tmp_path / "instrument" / "mlp.joblib"
    path.parent.mkdir(parents=True)
    path.with_suffix(path.suffix + ".active.json").write_text(
        '{"format_version":2,"artifact":"model.joblib"}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported horizon model pointer"):
        HorizonMLPExpert.active_artifact_path(path)


def test_inline_training_marks_an_already_seen_sample_without_update(tmp_path, monkeypatch):
    store = HorizonLearningStore(tmp_path / "state")
    root = tmp_path / "models"
    horizons = [{"id": 0, "seconds": 30, "models": ["mlp"]}]
    store.observe_event(
        event_id="seen-first",
        symbol="BTC/USDT",
        event_ts=100.0,
        close=100.0,
        features={"price": 100.0, "volume": 1.0},
        horizons=horizons,
        model_root=root,
        defer_training=True,
    )
    monkeypatch.setattr(HorizonMLPExpert, "partial_fit", lambda *args, **kwargs: False)
    result = store.observe_event(
        event_id="seen-maturity",
        symbol="BTC/USDT",
        event_ts=130.0,
        close=101.0,
        features={"price": 101.0, "volume": 1.0},
        horizons=horizons,
        model_root=root,
        defer_training=False,
    )
    assert result["matured"] == 1
    assert result["updated"] == 0
    assert result["rejected"] == 0


def test_deferred_training_does_not_count_a_duplicate_model_update(tmp_path, monkeypatch):
    store = HorizonLearningStore(tmp_path / "state")
    root = tmp_path / "models"
    horizons = [{"id": 0, "seconds": 30, "models": ["mlp"]}]
    for event_id, timestamp, close in (
        ("deferred-seen-first", 100.0, 100.0),
        ("deferred-seen-maturity", 130.0, 101.0),
    ):
        store.observe_event(
            event_id=event_id,
            symbol="BTC/USDT",
            event_ts=timestamp,
            close=close,
            features={"price": close, "volume": 1.0},
            horizons=horizons,
            model_root=root,
            defer_training=True,
        )
    monkeypatch.setattr(HorizonMLPExpert, "partial_fit", lambda *args, **kwargs: False)
    result = store.train_pending(
        symbol="BTC/USDT",
        horizons=horizons,
        model_root=root,
    )
    assert result == {"updated": 0, "rejected": 0, "attempted": 1}
