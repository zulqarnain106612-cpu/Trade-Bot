"""Horizon model training, durable labels, and challenger lifecycle tests.

Decides:
  - MODL-001 — model training and artifact lifecycle requirements
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from types import SimpleNamespace

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
        ("horizons:\n  short: {id: 0, seconds: 30, models: [tft]}\n", "without a compatible trainer"),
        ("horizons:\n  short: {id: 0, seconds: 30, models: [mlp, mlp]}\n", "without a compatible trainer"),
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
