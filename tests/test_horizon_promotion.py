from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.models.horizon_learning import HorizonLearningStore
from src.models.horizon_mlp import HorizonMLPExpert


def _promotion_fixture(tmp_path: Path) -> tuple[HorizonLearningStore, Path, str, str, str]:
    symbol = "BTC/USDT"
    instrument = hashlib.sha256(symbol.encode()).hexdigest()[:16]
    root = tmp_path / "models"
    base = root / "h2" / instrument
    active_path = base / "mlp.joblib"
    shadow_path = base / "shadow" / "mlp.joblib"

    incumbent = HorizonMLPExpert(("price", "volume"), horizon_id=1, instrument_hash=instrument)
    challenger = HorizonMLPExpert(("price", "volume"), horizon_id=1, instrument_hash=instrument)
    for index in range(3):
        features = {"price": 100.0 + index, "volume": 1.0}
        incumbent.partial_fit(features, 0.01 * index, sample_id=f"inc-{index}")
        challenger.partial_fit(features, 0.01 * index, sample_id=f"new-{index}")
    incumbent.save(active_path)
    incumbent_pointer = HorizonMLPExpert.activation_pointer(active_path)
    incumbent_pointer["lifecycle_status"] = "PROMOTED"
    HorizonMLPExpert.write_activation_pointer(active_path, incumbent_pointer)
    challenger.save(shadow_path)
    incumbent_version = str(incumbent.artifact_sha256)
    candidate_version = str(challenger.artifact_sha256)

    store = HorizonLearningStore(root)
    horizon = [{"id": 1, "seconds": 120, "models": ["mlp"]}]
    features_hash = store._features({"price": 100.0, "volume": 1.0})[1]
    # The production gate requires 100 paired OOS forecasts. The supported
    # 2-minute target matures on the second subsequent one-minute source event.
    # Two unlabeled events mature the final forecast without manufacturing a prediction.
    base_ts = time.time() - 102 * 60.0 - 600.0
    for index in range(100):
        timestamp = base_ts + 60.0 * index
        close = 100.0 + index
        event_id = f"forward-{index}"
        event_features = {"price": close, "volume": 1.0}
        store.observe_event(
            event_id=event_id,
            symbol=symbol,
            event_ts=timestamp,
            close=close,
            features=event_features,
            horizons=horizon,
            model_root=root,
            defer_training=True,
            source_cadence_seconds=60.0,
            source_timeframe="1m",
        )
        store.record_predictions(
            event_id=event_id,
            symbol=symbol,
            predictions=[
                SimpleNamespace(
                    status="PROMOTED",
                    error=None,
                    horizon_id=1,
                    magnitude_mu=0.005,
                    prediction_ts=timestamp,
                    model_version=incumbent_version,
                    feature_schema_hash=features_hash,
                    candidate_magnitude_mu=0.01,
                    candidate_model_version=candidate_version,
                    candidate_schema_hash=features_hash,
                )
            ],
        )
    for index in (100, 101):
        close = 200.0 + index
        store.observe_event(
            event_id=f"forward-label-flush-{index}",
            symbol=symbol,
            event_ts=base_ts + 60.0 * index,
            close=close,
            features={"price": close, "volume": 1.0},
            horizons=horizon,
            model_root=root,
            defer_training=True,
            source_cadence_seconds=60.0,
            source_timeframe="1m",
        )

    passing = store.evaluate_candidate(
        symbol=symbol,
        horizon_id=1,
        source_timeframe="1m",
        candidate_version=candidate_version,
        incumbent_version=incumbent_version,
        minimum_samples=100,
        max_evidence_age_seconds=1000,
        now=time.time(),
    )
    assert passing["eligible"]
    return store, root, symbol, candidate_version, incumbent_version


def test_horizon_activation_requires_paired_evaluation_and_is_reversible(tmp_path: Path) -> None:
    store, root, symbol, candidate_version, incumbent_version = _promotion_fixture(tmp_path)
    component_id = "model:horizon/1/BTCUSDT/candidate/test"
    instrument = hashlib.sha256(symbol.encode()).hexdigest()[:16]
    active_path = root / "h2" / instrument / "mlp.joblib"

    store.activate_candidate(
        component_id=component_id,
        symbol=symbol,
        horizon_id=1,
        candidate_version=candidate_version,
        model_root=root,
    )
    assert HorizonMLPExpert.activation_state(active_path) == "PROBATION"
    assert HorizonMLPExpert.load(active_path).artifact_sha256 == candidate_version

    store.complete_promotion(component_id=component_id, model_root=root)
    assert HorizonMLPExpert.activation_state(active_path) == "PROMOTED"

    # A second component can reuse the already materialized content-addressed
    # version instead of copying the same artifact directory again.
    repeated_component = f"{component_id}/reuse"
    store.activate_candidate(
        component_id=repeated_component,
        symbol=symbol,
        horizon_id=1,
        candidate_version=candidate_version,
        model_root=root,
    )
    assert HorizonMLPExpert.activation_state(active_path) == "PROBATION"
    store.rollback_promotion(component_id=repeated_component, model_root=root)
    assert HorizonMLPExpert.activation_state(active_path) == "PROMOTED"

    store.rollback_promotion(component_id=component_id, model_root=root)
    assert HorizonMLPExpert.load(active_path).artifact_sha256 == incumbent_version
    assert HorizonMLPExpert.activation_state(active_path) == "PROMOTED"


def test_candidate_evaluation_continues_when_paired_sample_count_is_low(tmp_path: Path) -> None:
    store, _, symbol, candidate_version, incumbent_version = _promotion_fixture(tmp_path)
    evaluation = store.evaluate_candidate(
        symbol=symbol,
        horizon_id=1,
        source_timeframe="1m",
        candidate_version=candidate_version,
        incumbent_version=incumbent_version,
        minimum_samples=101,
        max_evidence_age_seconds=1000,
    )
    assert not evaluation["eligible"]
    assert evaluation["outcome"] == "CONTINUE_SHADOW"
    assert evaluation["reason"] == "insufficient paired forward samples"


def test_candidate_evaluation_never_approves_stale_evidence(tmp_path: Path) -> None:
    store, _, symbol, candidate_version, incumbent_version = _promotion_fixture(tmp_path)
    evaluation = store.evaluate_candidate(
        symbol=symbol,
        horizon_id=1,
        source_timeframe="1m",
        candidate_version=candidate_version,
        incumbent_version=incumbent_version,
        minimum_samples=100,
        max_evidence_age_seconds=1,
        now=time.time() + 100.0,
    )
    assert not evaluation["eligible"]
    assert evaluation["outcome"] == "CONTINUE_SHADOW"
    assert evaluation["reason"] == "paired evaluation evidence is stale"


def test_interrupted_horizon_probation_rolls_back_on_restart(tmp_path: Path) -> None:
    store, root, symbol, candidate_version, incumbent_version = _promotion_fixture(tmp_path)
    component_id = "model:horizon/1/BTCUSDT/candidate/recovery"
    instrument = hashlib.sha256(symbol.encode()).hexdigest()[:16]
    active_path = root / "h2" / instrument / "mlp.joblib"
    store.activate_candidate(
        component_id=component_id,
        symbol=symbol,
        horizon_id=1,
        candidate_version=candidate_version,
        model_root=root,
    )

    recovered = store.recover_promotions(root)
    assert recovered == [component_id]
    assert HorizonMLPExpert.load(active_path).artifact_sha256 == incumbent_version
    assert HorizonMLPExpert.activation_state(active_path) == "PROMOTED"


def test_concurrent_horizon_promotions_allow_only_one_activation(tmp_path: Path) -> None:
    store, root, symbol, candidate_version, _ = _promotion_fixture(tmp_path)

    def activate(component_id: str) -> str:
        try:
            store.activate_candidate(
                component_id=component_id,
                symbol=symbol,
                horizon_id=1,
                candidate_version=candidate_version,
                model_root=root,
            )
            return "activated"
        except ValueError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(activate, ("model:horizon/a", "model:horizon/b")))
    assert sorted(outcomes) == ["activated", "refused"]


def test_recovery_completes_a_promoted_pointer_and_ignores_unmatched_pointer(tmp_path):
    store, root, symbol, candidate_version, _ = _promotion_fixture(tmp_path)
    component_id = "model:horizon/recovery-promoted"
    instrument = hashlib.sha256(symbol.encode()).hexdigest()[:16]
    active_path = root / "h2" / instrument / "mlp.joblib"
    store.activate_candidate(
        component_id=component_id,
        symbol=symbol,
        horizon_id=1,
        candidate_version=candidate_version,
        model_root=root,
    )
    store.complete_promotion(component_id=component_id, model_root=root)
    with store._connect() as db:
        db.execute(
            "INSERT INTO horizon_promotions "
            "(component_id, symbol, horizon_id, candidate_version, previous_pointer_json, state, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, 'PROMOTED', ?)",
            ("model:horizon/unmatched", symbol, 1, "not-the-active-version", time.time()),
        )
    assert store.recover_promotions(root) == [component_id]
    assert HorizonMLPExpert.activation_state(active_path) == "PROMOTED"


def test_rollback_without_a_recorded_promotion_is_refused(tmp_path):
    store = HorizonLearningStore(tmp_path / "state")
    with pytest.raises(ValueError, match="no reversible horizon promotion"):
        store.rollback_promotion(component_id="missing", model_root=tmp_path / "models")


def test_rollback_removes_the_candidate_pointer_when_no_incumbent_existed(tmp_path):
    store, root, symbol, candidate_version, _ = _promotion_fixture(tmp_path)
    instrument = hashlib.sha256(symbol.encode()).hexdigest()[:16]
    active_path = root / "h2" / instrument / "mlp.joblib"
    active_pointer = active_path.with_suffix(active_path.suffix + ".active.json")
    active_pointer.unlink()

    component_id = "model:horizon/no-incumbent"
    store.activate_candidate(
        component_id=component_id,
        symbol=symbol,
        horizon_id=1,
        candidate_version=candidate_version,
        model_root=root,
    )
    assert HorizonMLPExpert.activation_state(active_path) == "PROBATION"

    store.rollback_promotion(component_id=component_id, model_root=root)

    assert not active_pointer.exists()
    assert HorizonMLPExpert.activation_state(active_path) == "UNPROMOTED"


def test_activation_refuses_ineligible_candidate_without_touching_active_pointer(
    tmp_path, monkeypatch
):
    store = HorizonLearningStore(tmp_path / "state")
    monkeypatch.setattr(
        store,
        "evaluate_candidate",
        lambda **kwargs: {"eligible": False, "candidate_model_version": "candidate"},
    )
    with pytest.raises(ValueError, match="no current, passing paired evaluation"):
        store.activate_candidate(
            component_id="model:horizon/ineligible",
            symbol="BTC/USDT",
            horizon_id=0,
            candidate_version="candidate",
            model_root=tmp_path / "models",
        )


def test_activation_refuses_candidate_whose_artifact_hash_changed(tmp_path, monkeypatch):
    from types import SimpleNamespace

    store, root, symbol, candidate_version, _ = _promotion_fixture(tmp_path)
    monkeypatch.setattr(
        store,
        "evaluate_candidate",
        lambda **kwargs: {
            "eligible": True,
            "candidate_model_version": candidate_version,
        },
    )
    monkeypatch.setattr(
        HorizonMLPExpert,
        "load",
        staticmethod(lambda *args, **kwargs: SimpleNamespace(artifact_sha256="changed")),
    )
    with pytest.raises(ValueError, match="artifact changed after its evaluation"):
        store.activate_candidate(
            component_id="model:horizon/changed-artifact",
            symbol=symbol,
            horizon_id=1,
            candidate_version=candidate_version,
            model_root=root,
        )


def test_activation_refuses_a_second_open_promotion_for_the_same_component(tmp_path):
    store, root, symbol, candidate_version, _ = _promotion_fixture(tmp_path)
    component_id = "model:horizon/duplicate-open"
    args = {
        "component_id": component_id,
        "symbol": symbol,
        "horizon_id": 1,
        "candidate_version": candidate_version,
        "model_root": root,
    }
    store.activate_candidate(**args)
    with pytest.raises(ValueError, match="already active"):
        store.activate_candidate(**args)


def test_complete_promotion_refuses_missing_or_changed_active_pointer(tmp_path):
    store = HorizonLearningStore(tmp_path / "empty-state")
    with pytest.raises(ValueError, match="not in promotion probation"):
        store.complete_promotion(component_id="missing", model_root=tmp_path / "empty-models")

    store, root, symbol, candidate_version, _ = _promotion_fixture(tmp_path / "pointer")
    component_id = "model:horizon/pointer-changed"
    store.activate_candidate(
        component_id=component_id,
        symbol=symbol,
        horizon_id=1,
        candidate_version=candidate_version,
        model_root=root,
    )
    instrument = hashlib.sha256(symbol.encode()).hexdigest()[:16]
    active_path = root / "h2" / instrument / "mlp.joblib"
    pointer = HorizonMLPExpert.activation_pointer(active_path)
    pointer["sha256"] = "replaced-by-another-version"
    HorizonMLPExpert.write_activation_pointer(active_path, pointer)
    with pytest.raises(ValueError, match="pointer changed before promotion completion"):
        store.complete_promotion(component_id=component_id, model_root=root)


def test_activation_cleans_temporary_directory_after_concurrent_version_creation(
    tmp_path, monkeypatch
):
    import os
    import shutil

    import src.models.horizon_learning as learning_module

    store, root, symbol, candidate_version, _ = _promotion_fixture(tmp_path)
    instrument = hashlib.sha256(symbol.encode()).hexdigest()[:16]
    active_path = root / "h2" / instrument / "mlp.joblib"
    original_replace = os.replace

    def simulate_concurrent_version(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.is_dir() and source_path.name.startswith(".horizon-activate-"):
            shutil.copytree(source_path, destination_path)
            raise FileExistsError("concurrent activator created the version directory")
        return original_replace(source, destination)

    monkeypatch.setattr(learning_module.os, "replace", simulate_concurrent_version)
    store.activate_candidate(
        component_id="model:horizon/concurrent-activation",
        symbol=symbol,
        horizon_id=1,
        candidate_version=candidate_version,
        model_root=root,
    )
    versions = active_path.parent / f".{active_path.name}.versions"
    assert HorizonMLPExpert.artifact_available(active_path)
    assert not list(versions.glob(".horizon-activate-*"))
