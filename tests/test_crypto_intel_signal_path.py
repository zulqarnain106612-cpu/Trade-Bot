"""Covers horizon dispatch, honest abstention, and the risk-gated signal path.

Decides:
  - REG-0031 — Horizon drift detection suppresses the signal and is persisted as measured
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from src.features.mempool import MempoolFeatures
from src.features.onchain import OnChainFeatures
from src.intel import CryptoIntelligence
from src.workers.orchestrator import WorkerResult


class _FakeOrchestrator:
    """Records submitted tasks; never spawns processes."""

    def __init__(self) -> None:
        self.submitted: list[object] = []
        self.started = False
        self.shutdown_called = False

    def start(self) -> None:
        self.started = True

    def submit(self, task) -> None:
        self.submitted.append(task)

    def shutdown(self) -> None:
        self.shutdown_called = True


def _result(
    horizon_id: int,
    direction: int = 1,
    confidence: float = 0.9,
    status: str = "UNTRAINED",
) -> WorkerResult:
    return WorkerResult(
        task_id=f"t{horizon_id}",
        horizon_id=horizon_id,
        direction=direction,
        confidence=confidence,
        magnitude_mu=0.01 * direction,
        magnitude_sigma=0.002,
        timing=0.5,
        algo="IOC",
        status=status,
        model_id=f"model-{horizon_id}" if status != "UNTRAINED" else None,
        model_version=f"version-{horizon_id}" if status != "UNTRAINED" else None,
        prediction_ts=time.time(),
        oos_score=0.75 if status != "UNTRAINED" else None,
        oos_sample_count=100 if status != "UNTRAINED" else 0,
        confidence_calibrated=status != "UNTRAINED",
        uncertainty_calibrated=status != "UNTRAINED",
    )


def _risk_context() -> dict[str, float]:
    return {
        "atr": 500.0,
        "cvar": 0.02,
        "drawdown": 0.0,
        "daily_loss": 0.0,
        "odds": 2.0,
        "as_of_ts": time.time(),
    }


@pytest.fixture
def intel(tmp_path, monkeypatch):
    cfg = tmp_path / "intelligence.yaml"
    cfg.write_text(
        f"n_workers: 1\nn_horizons: 3\nduckdb_path: {tmp_path / 'intel.duckdb'}\n",
    )
    horizons = tmp_path / "horizons.yaml"
    horizons.write_text(
        "horizons:\n"
        "  short: {id: 0, label: 30s, seconds: 30, models: [mlp]}\n"
        "  medium: {id: 1, label: 2m, seconds: 120, models: [mlp]}\n"
        "  long: {id: 2, label: 5m, seconds: 300, models: [mlp]}\n"
    )
    monkeypatch.setattr("src.intel._HORIZONS_CONFIG", horizons)
    monkeypatch.setenv("DUCKDB_PATH", str(tmp_path / "intel.duckdb"))
    obj = CryptoIntelligence(config_path=cfg)
    obj._orchestrator = _FakeOrchestrator()

    async def _mempool(*_a, **_kw):
        return MempoolFeatures(
            tx_count=10,
            fee_rate_p50_sat=5.0,
            fee_rate_p90_sat=20.0,
            mempool_bytes=1000,
            fee_pressure=0.02,
        )

    async def _onchain(*_a, **_kw):
        return OnChainFeatures(sopr=1.01, nvt=50.0, mvrv=1.5)

    obj._fetch_mempool = _mempool
    obj._onchain.compute = _onchain
    yield obj
    obj.close()


def _ohlcv() -> dict:
    return {"close": 50000.0, "volume": 12.0, "atr": 500.0}


def _deliver_results(intel, results: list[WorkerResult]) -> None:
    def drain(timeout: float = 0.0):
        del timeout
        # Source-cadence validation may deliberately avoid submitting a horizon
        # whose target is shorter than the configured market-data interval.
        # Return a result only for a task that production actually submitted.
        submitted = {task.horizon_id: task for task in intel._orchestrator.submitted}
        for index, result in enumerate(results):
            task = submitted.get(result.horizon_id)
            if task is None:
                continue
            del results[index]
            result.task_id = task.task_id
            return result
        return None

    intel._drain_one = drain


def test_on_bar_abstains_when_horizon_models_are_untrained(intel):
    results = [_result(i) for i in range(3)]
    _deliver_results(intel, results)

    sig = asyncio.run(intel.on_bar("BTC/USDT", _ohlcv(), regime_id=2))

    assert sig is None  # declared models are untrained and therefore abstain
    # one task submitted per configured horizon
    assert len(intel._orchestrator.submitted) == 3


def test_on_bar_does_not_treat_regime_confidence_as_model_evidence(intel):
    results = [_result(i) for i in range(3)]
    _deliver_results(intel, results)

    sig = asyncio.run(
        intel.on_bar("BTC/USDT", _ohlcv(), regime_confidences=[0.9, 0.5, 0.1]),
    )
    assert sig is None


def test_on_bar_updates_granger_when_alt_price_history_is_long_enough(intel):
    results = [_result(i) for i in range(3)]
    _deliver_results(intel, results)
    seen: dict[str, object] = {}
    intel._granger.update = lambda btc_ret, alts: seen.update(n=len(btc_ret), alts=alts)

    ohlcv = _ohlcv() | {"close_history": [50000.0 + i for i in range(11)]}
    asyncio.run(
        intel.on_bar("BTC/USDT", ohlcv, alt_prices={"ETH/USDT": [10.0 + i for i in range(12)]}),
    )
    assert seen["n"] == 10


def test_on_bar_returns_none_when_no_worker_results_arrive(intel, monkeypatch):
    intel._drain_one = lambda timeout: None
    # keep the 5s collection deadline from actually elapsing
    monkeypatch.setattr("time.monotonic", lambda: 1e18)

    assert asyncio.run(intel.on_bar("BTC/USDT", _ohlcv())) is None


def test_on_bar_returns_none_when_horizons_resolve_to_flat(intel):
    results = [
        _result(0, direction=1, status="PROMOTED"),
        _result(1, direction=-1, status="PROMOTED"),
        _result(2, direction=-1, status="PROMOTED"),
    ]
    _deliver_results(intel, results)

    assert asyncio.run(intel.on_bar("BTC/USDT", _ohlcv())) is None


def test_horizon_status_exposes_stale_predictions_and_unpromoted_candidates(intel, monkeypatch):
    from src.models.horizon_mlp import HorizonMLPExpert

    intel._last_horizon_symbol = "BTC/USDT"
    intel._source_timeframe = "1m"
    intel._started = True
    intel._cfg["max_prediction_age_seconds"] = 20.0
    intel._last_horizon_results[0] = (_result(0, status="PROMOTED"), time.time() - 100.0)
    monkeypatch.setattr(
        intel._learning_store,
        "stats",
        lambda **kwargs: {0: {"training_samples": 1}},
    )
    monkeypatch.setattr(
        intel._learning_store,
        "latest_candidate_evaluation",
        lambda **kwargs: {"eligible": False, "reason": "stale evidence"},
    )

    def artifact_available(path):
        if "candidate" in path.parts or "shadow" in path.parts:
            return True
        return "h1" in path.parts

    monkeypatch.setattr(HorizonMLPExpert, "artifact_available", staticmethod(artifact_available))
    monkeypatch.setattr(HorizonMLPExpert, "activation_state", staticmethod(lambda path: "PROMOTED"))

    rows = intel.horizon_status()
    by_id = {row["id"]: row for row in rows}
    assert by_id[0]["status"] == "STALE"
    assert by_id[0]["active_lifecycle_state"] == "PROMOTED"
    assert by_id[0]["candidate_evaluation"]["reason"] == "stale evidence"
    assert by_id[1]["status"] == "DATA_PENDING"
    assert by_id[1]["lifecycle_state"] == "CANDIDATE_UNPROMOTED"


def test_constructor_logs_recovered_horizon_promotions(tmp_path, monkeypatch):
    from src.intel import CryptoIntelligence
    from src.models.horizon_learning import HorizonLearningStore

    horizons = tmp_path / "horizons.yaml"
    horizons.write_text(
        "horizons:\n"
        "  short: {id: 0, label: 30s, seconds: 30, models: [mlp]}\n"
        "  medium: {id: 1, label: 2m, seconds: 120, models: [mlp]}\n"
        "  long: {id: 2, label: 5m, seconds: 300, models: [mlp]}\n"
    )
    monkeypatch.setattr("src.intel._HORIZONS_CONFIG", horizons)
    monkeypatch.setattr(
        HorizonLearningStore, "recover_promotions", lambda self, root: ["recovered-component"]
    )
    config = tmp_path / "recovery-intelligence.yaml"
    config.write_text(
        f"n_workers: 1\n"
        f"duckdb_path: {tmp_path / 'recovery.duckdb'}\n"
        f"checkpoint_dir: {tmp_path / 'recovery-models'}\n"
    )
    intel = CryptoIntelligence(config_path=config)
    try:
        assert intel.checkpoint_dir.exists()
    finally:
        intel.close()


def test_on_bar_returns_none_when_the_circuit_breaker_trips(intel, monkeypatch):
    results = [_result(i, status="PROMOTED") for i in range(3)]
    _deliver_results(intel, results)
    monkeypatch.setattr(
        intel._learning_store,
        "stats",
        lambda symbol, source_timeframe: {
            horizon_id: {"oos_by_regime": {2: {"directional_accuracy": 0.75, "samples": 100}}}
            for horizon_id in range(3)
        },
    )
    intel._risk_gate.size = lambda **kwargs: SimpleNamespace(
        suppressed=False, size_pct=0.01, reason=None
    )
    intel._risk_gate.circuit_breaker = lambda drawdown, daily_loss: True

    assert (
        asyncio.run(intel.on_bar("BTC/USDT", _ohlcv(), regime_id=2, risk_context=_risk_context()))
        is None
    )
    assert "circuit breaker" in intel.last_decision["abstain_reason"]


def test_on_bar_returns_none_when_the_risk_gate_suppresses_the_size(intel, monkeypatch):
    results = [_result(i, confidence=0.01, status="PROMOTED") for i in range(3)]
    _deliver_results(intel, results)
    monkeypatch.setattr(
        intel._learning_store,
        "stats",
        lambda symbol, source_timeframe: {
            horizon_id: {"oos_by_regime": {2: {"directional_accuracy": 0.75, "samples": 100}}}
            for horizon_id in range(3)
        },
    )
    intel._risk_gate.size = lambda **kwargs: SimpleNamespace(
        suppressed=True, size_pct=0.0, reason="test size suppression"
    )

    assert (
        asyncio.run(intel.on_bar("BTC/USDT", _ohlcv(), regime_id=2, risk_context=_risk_context()))
        is None
    )
    assert "risk sizing suppressed" in intel.last_decision["abstain_reason"]


def test_on_bar_abstains_when_risk_context_is_missing_or_stale(intel, monkeypatch):
    _deliver_results(intel, [_result(i, status="PROMOTED") for i in range(3)])
    monkeypatch.setattr(
        intel._learning_store,
        "stats",
        lambda symbol, source_timeframe: {
            horizon_id: {"oos_by_regime": {2: {"directional_accuracy": 0.75, "samples": 100}}}
            for horizon_id in range(3)
        },
    )
    missing = asyncio.run(intel.on_bar("BTC/USDT", _ohlcv(), regime_id=2))
    assert missing is None
    assert "risk context unavailable" in intel.last_decision["abstain_reason"]

    stale = {**_risk_context(), "as_of_ts": time.time() - 1000.0}
    _deliver_results(intel, [_result(i, status="PROMOTED") for i in range(3)])
    result = asyncio.run(intel.on_bar("BTC/USDT", _ohlcv(), regime_id=2, risk_context=stale))
    assert result is None
    assert "stale or outside valid bounds" in intel.last_decision["abstain_reason"]


def test_on_bar_persists_timestamped_learning_and_emits_only_after_risk_gate(intel, monkeypatch):
    results = [_result(i, status="PROMOTED") for i in range(3)]
    _deliver_results(intel, results)
    store = intel._learning_store
    scheduled = []
    writes = []
    calls = []
    monkeypatch.setattr(store, "source_cadence_seconds", lambda symbol, timeframe: 60.0)
    monkeypatch.setattr(
        store,
        "observe_event",
        lambda **kwargs: {"updated": 1, "matured": 0, "rejected": 0},
    )
    monkeypatch.setattr(
        store,
        "stats",
        lambda symbol, source_timeframe: {
            horizon_id: {"oos_by_regime": {2: {"directional_accuracy": 0.75, "samples": 100}}}
            for horizon_id in range(3)
        },
    )
    monkeypatch.setattr(store, "record_predictions", lambda **kwargs: calls.append("record"))
    monkeypatch.setattr(store, "evaluate_candidate", lambda **kwargs: calls.append("evaluate"))
    monkeypatch.setattr(intel, "_schedule_training", lambda symbol, specs: scheduled.append(symbol))
    intel._risk_gate.size = lambda **kwargs: SimpleNamespace(
        suppressed=False, size_pct=0.02, reason=None
    )
    intel._risk_gate.circuit_breaker = lambda drawdown, daily_loss: False
    intel._risk_gate.check_drift = lambda *args: None
    intel._duckdb.write_horizon_metric = lambda **kwargs: writes.append(kwargs)

    signal = asyncio.run(
        intel.on_bar(
            "BTC/USDT",
            _ohlcv(),
            regime_id=2,
            event_ts=1_700_000_000_000.0,
            event_id="coverage-event-1",
            source_timeframe="1m",
            risk_context=_risk_context(),
        )
    )

    assert signal is not None
    assert signal.direction == 1
    assert signal.regime_id == 2
    assert intel.learning_status["updated"] == 1
    assert intel.last_horizon_symbol == "BTC/USDT"
    assert intel.last_horizon_ts is not None
    assert intel.checkpoint_dir == intel._checkpoint_dir
    assert intel.worker_pool is intel._orchestrator
    assert intel.upgrade_registry is intel._registry
    assert calls == ["record", "evaluate", "evaluate", "evaluate"]
    assert scheduled == ["BTC/USDT"]
    assert len(writes) == 1
    assert writes[0]["drift_detected"] is False


def test_detected_drift_suppresses_the_horizon_signal_and_is_persisted(intel, monkeypatch):
    results = [_result(i, status="PROMOTED") for i in range(3)]
    _deliver_results(intel, results)
    store = intel._learning_store
    scheduled = []
    writes = []
    calls = []
    monkeypatch.setattr(store, "source_cadence_seconds", lambda symbol, timeframe: 60.0)
    monkeypatch.setattr(
        store,
        "observe_event",
        lambda **kwargs: {"updated": 1, "matured": 0, "rejected": 0},
    )
    monkeypatch.setattr(
        store,
        "stats",
        lambda symbol, source_timeframe: {
            horizon_id: {"oos_by_regime": {2: {"directional_accuracy": 0.75, "samples": 100}}}
            for horizon_id in range(3)
        },
    )
    monkeypatch.setattr(store, "record_predictions", lambda **kwargs: calls.append("record"))
    monkeypatch.setattr(store, "evaluate_candidate", lambda **kwargs: calls.append("evaluate"))
    monkeypatch.setattr(intel, "_schedule_training", lambda symbol, specs: scheduled.append(symbol))
    intel._risk_gate.size = lambda **kwargs: SimpleNamespace(
        suppressed=False, size_pct=0.02, reason=None
    )
    intel._risk_gate.circuit_breaker = lambda drawdown, daily_loss: False
    intel._risk_gate.check_drift = lambda *args: True
    intel._duckdb.write_horizon_metric = lambda **kwargs: writes.append(kwargs)

    signal = asyncio.run(
        intel.on_bar(
            "BTC/USDT",
            _ohlcv(),
            regime_id=2,
            event_ts=1_700_000_000_000.0,
            event_id="coverage-event-1",
            source_timeframe="1m",
            risk_context=_risk_context(),
        )
    )

    assert signal is None
    assert intel._last_decision.action == "ABSTAIN"
    assert "drift" in intel._last_decision.abstain_reason
    assert len(writes) == 1 and writes[0]["drift_detected"] is True


def test_on_bar_clears_previous_symbol_results_and_cadence(intel, monkeypatch):
    _deliver_results(intel, [_result(i) for i in range(3)])
    intel._last_horizon_symbol = "ETH/USDT"
    intel._last_horizon_results[99] = (_result(99), time.time())
    intel._source_cadence_seconds = 60.0
    monkeypatch.setattr(intel._learning_store, "source_cadence_seconds", lambda *_: None)

    assert asyncio.run(intel.on_bar("BTC/USDT", _ohlcv())) is None

    assert 99 not in intel._last_horizon_results
    assert intel._source_cadence_seconds is None


def test_on_bar_handles_millisecond_event_with_unknown_observed_cadence(intel, monkeypatch):
    store = intel._learning_store
    seen = []
    monkeypatch.setattr(store, "source_cadence_seconds", lambda *_: None)
    monkeypatch.setattr(
        store,
        "observe_event",
        lambda **kwargs: seen.append(kwargs) or {"updated": 0, "matured": 0, "rejected": 0},
    )
    monkeypatch.setattr(store, "record_predictions", lambda **kwargs: 0)
    monkeypatch.setattr(store, "evaluate_candidate", lambda **kwargs: None)
    monkeypatch.setattr(intel, "_schedule_training", lambda *_: None)
    _deliver_results(intel, [_result(i) for i in range(3)])

    assert (
        asyncio.run(
            intel.on_bar(
                "BTC/USDT",
                _ohlcv(),
                event_ts=1_700_000_000_000.0,
                event_id="millisecond-event",
                source_timeframe="1m",
            )
        )
        is None
    )
    assert seen[0]["event_ts"] == 1_700_000_000.0
    assert intel.learning_status["last_error"] is None


def test_on_bar_keeps_the_final_regime_guard_even_after_a_decision(intel, monkeypatch):
    from src.fusion.horizon_decision import UnifiedHorizonDecision

    decision = UnifiedHorizonDecision(
        action="BUY",
        direction=1,
        expected_return=0.01,
        uncertainty=0.002,
        conflict=False,
        contributions={0: 1.0},
        model_versions={0: "v0"},
        freshness_seconds={0: 0.0},
        abstain_reason=None,
        regime_id=None,
    )
    monkeypatch.setattr(
        "src.fusion.horizon_decision.fuse_horizon_predictions", lambda *args, **kwargs: decision
    )
    intel._risk_gate.size = lambda **kwargs: SimpleNamespace(
        suppressed=False, size_pct=0.02, reason=None
    )
    intel._risk_gate.circuit_breaker = lambda **kwargs: False
    intel._risk_gate.check_drift = lambda *args: None
    intel._duckdb.write_horizon_metric = lambda **kwargs: None
    _deliver_results(intel, [_result(i, status="PROMOTED") for i in range(3)])

    assert asyncio.run(intel.on_bar("BTC/USDT", _ohlcv(), risk_context=_risk_context())) is None


def test_on_bar_invalid_event_timestamp_does_not_schedule_a_label(intel):
    results = [_result(i) for i in range(3)]
    _deliver_results(intel, results)

    assert asyncio.run(intel.on_bar("BTC/USDT", _ohlcv(), event_ts=float("nan"))) is None
    assert "invalid source event timestamp" in intel.learning_status["last_error"]


def test_on_bar_learning_store_error_is_reported_and_does_not_emit_a_signal(intel, monkeypatch):
    results = [_result(i) for i in range(3)]
    _deliver_results(intel, results)

    def fail_observe(**kwargs):
        raise ValueError("source event rejected")

    monkeypatch.setattr(intel._learning_store, "source_cadence_seconds", lambda *_: 60.0)
    monkeypatch.setattr(intel._learning_store, "observe_event", fail_observe)
    monkeypatch.setattr(intel._learning_store, "record_predictions", lambda **kwargs: 0)
    monkeypatch.setattr(intel._learning_store, "evaluate_candidate", lambda **kwargs: None)
    monkeypatch.setattr(intel, "_schedule_training", lambda *_: None)

    assert (
        asyncio.run(
            intel.on_bar(
                "BTC/USDT",
                _ohlcv(),
                event_ts=1_700_000_000.0,
                event_id="coverage-event-error",
                source_timeframe="1m",
            )
        )
        is None
    )
    assert intel.learning_status["last_error"] == "source event rejected"


def test_on_bar_worker_submission_failure_is_an_explicit_abstention(intel):
    def fail_submit(task):
        raise RuntimeError("worker unavailable")

    intel._orchestrator.submit = fail_submit
    result = asyncio.run(intel.on_bar("BTC/USDT", _ohlcv(), regime_id=2))
    assert result is None
    assert intel.last_decision["action"] == "ABSTAIN"


class TestDrainOne:
    def test_returns_none_and_logs_when_collect_raises(self, intel):
        def _boom(timeout):
            raise RuntimeError("queue closed")

        intel._orchestrator.collect = _boom
        assert intel._drain_one(timeout=0.0) is None

    def test_folds_an_ecc_payload_into_state_and_returns_none(self, intel):
        intel._orchestrator.collect = lambda timeout: {
            "type": "ecc",
            "result": {"cluster_flow_score": 0.9},
        }
        assert intel._drain_one(timeout=0.0) is None
        assert intel._ecc_state == {"cluster_flow_score": 0.9}

    def test_ignores_a_non_ecc_dict(self, intel):
        intel._orchestrator.collect = lambda timeout: {"type": "other"}
        assert intel._drain_one(timeout=0.0) is None
        assert intel._ecc_state == {}

    def test_returns_worker_results_unchanged(self, intel):
        res = _result(1)
        intel._orchestrator.collect = lambda timeout: res
        assert intel._drain_one(timeout=0.0) is res

    def test_drops_anything_else(self, intel):
        intel._orchestrator.collect = lambda timeout: "junk"
        assert intel._drain_one(timeout=0.0) is None


def test_on_bar_ignores_unmatched_worker_results_until_deadline(intel, monkeypatch):
    intel._cfg["inference_timeout_seconds"] = 0.05
    monkeypatch.setattr(
        intel._orchestrator,
        "submit",
        lambda task: f"expected-task-{task.horizon_id}",
    )
    stale = _result(99)
    stale.task_id = "__not-an-expected-task__"
    calls = {"count": 0}

    def drain_one(timeout):
        calls["count"] += 1
        return stale if calls["count"] == 1 else None

    monkeypatch.setattr(intel, "_drain_one", drain_one)
    assert (
        asyncio.run(intel.on_bar("BTC/USDT", _ohlcv(), regime_id=2, risk_context=_risk_context()))
        is None
    )
    assert calls["count"] >= 1
    assert intel.last_decision["action"] == "ABSTAIN"


def test_on_bar_reports_prediction_persistence_and_evaluation_errors(intel, monkeypatch):
    _deliver_results(intel, [_result(i) for i in range(3)])
    store = intel._learning_store
    monkeypatch.setattr(store, "source_cadence_seconds", lambda *_: 60.0)
    monkeypatch.setattr(
        store, "observe_event", lambda **kwargs: {"updated": 0, "matured": 0, "rejected": 0}
    )

    def fail_record(**kwargs):
        raise RuntimeError("prediction write failed")

    def fail_evaluation(**kwargs):
        raise RuntimeError("candidate evaluation failed")

    monkeypatch.setattr(store, "record_predictions", fail_record)
    monkeypatch.setattr(store, "evaluate_candidate", fail_evaluation)
    monkeypatch.setattr(intel, "_schedule_training", lambda *_: None)

    assert (
        asyncio.run(
            intel.on_bar(
                "BTC/USDT",
                _ohlcv(),
                event_ts=1_700_000_000.0,
                event_id="coverage-persistence-error",
                source_timeframe="1m",
            )
        )
        is None
    )
    assert intel.learning_status["last_error"] == "candidate evaluation failed"


def test_learning_status_collects_successful_and_failed_background_training(intel):
    class Completed:
        def done(self):
            return True

        def result(self):
            return {"updated": 2, "rejected": 1}

    class Failed:
        def done(self):
            return True

        def result(self):
            raise RuntimeError("background trainer failed")

    intel._training_future = Completed()
    status = intel.learning_status
    assert status["updated"] == 2
    assert status["rejected"] == 1
    assert status["training_active"] is False
    assert status["last_update"] is not None

    intel._training_future = Failed()
    status = intel.learning_status
    assert status["last_error"] == "background trainer failed"
    assert status["training_active"] is False

    class NoUpdates:
        def done(self):
            return True

        def result(self):
            return {"updated": 0, "rejected": 0}

    intel._learning_status = {"updated": 0, "rejected": 0, "last_update": None}
    intel._training_future = NoUpdates()
    status = intel.learning_status
    assert status["last_update"] is None


def test_schedule_training_submits_only_when_no_training_is_active(intel, monkeypatch):
    class Pending:
        def done(self):
            return False

    sentinel = Pending()
    submissions = []
    monkeypatch.setattr(
        intel._training_executor,
        "submit",
        lambda *args, **kwargs: submissions.append((args, kwargs)) or sentinel,
    )
    specs = {"short": {"id": 0, "seconds": 30, "models": ["mlp"]}}
    intel._schedule_training("BTC/USDT", specs)
    assert intel._training_future is sentinel
    assert len(submissions) == 1

    intel._schedule_training("BTC/USDT", specs)
    assert len(submissions) == 1


def test_collect_ecc_drops_a_stale_worker_result_from_a_previous_bar(intel):
    intel._orchestrator.collect = lambda timeout: _result(4)
    intel._ecc_state = {"cluster_flow_score": 0.4}

    assert intel._collect_ecc() == {"cluster_flow_score": 0.4}


def test_collect_ecc_marks_every_feature_invalid_when_the_snapshot_is_stale(intel, monkeypatch):
    intel._ecc_state = {
        "_validity": {"cluster": True, "mempool": True},
        "_observed_at": time.time() - 1000.0,
        "cluster_flow_score": 0.9,
    }
    monkeypatch.setattr(intel, "_ecc_max_age", 1.0)

    result = intel._collect_ecc()

    assert result["_validity"] == {"cluster": False, "mempool": False}


def test_close_survives_a_duckdb_that_fails_to_close(intel):
    def _boom():
        raise RuntimeError("locked")

    intel._duckdb.close = _boom
    intel.close()

    assert intel._started is False
    assert intel._orchestrator.shutdown_called is True
