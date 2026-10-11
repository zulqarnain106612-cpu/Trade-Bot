from __future__ import annotations

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

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
