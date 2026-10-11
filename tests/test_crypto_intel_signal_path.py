"""Covers horizon dispatch, honest abstention, and the risk-gated signal path."""

from __future__ import annotations

import asyncio
import time

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
    )


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
    def drain(_timeout):
        if not results:
            return None
        result = results.pop(0)
        task = next(
            task for task in intel._orchestrator.submitted if task.horizon_id == result.horizon_id
        )
        result.task_id = task.task_id
        return result

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


def test_on_bar_returns_none_when_the_circuit_breaker_trips(intel):
    results = [_result(i, status="PROMOTED") for i in range(3)]
    _deliver_results(intel, results)
    intel._risk_gate.circuit_breaker = lambda drawdown, daily_loss: True

    assert asyncio.run(intel.on_bar("BTC/USDT", _ohlcv())) is None


def test_on_bar_returns_none_when_the_risk_gate_suppresses_the_size(intel):
    # confidence below the gate's threshold -> suppressed, no signal
    results = [_result(i, confidence=0.01, status="PROMOTED") for i in range(3)]
    _deliver_results(intel, results)

    assert asyncio.run(intel.on_bar("BTC/USDT", _ohlcv())) is None


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


def test_collect_ecc_drops_a_stale_worker_result_from_a_previous_bar(intel):
    intel._orchestrator.collect = lambda timeout: _result(4)
    intel._ecc_state = {"cluster_flow_score": 0.4}

    assert intel._collect_ecc() == {"cluster_flow_score": 0.4}


def test_close_survives_a_duckdb_that_fails_to_close(intel):
    def _boom():
        raise RuntimeError("locked")

    intel._duckdb.close = _boom
    intel.close()

    assert intel._started is False
    assert intel._orchestrator.shutdown_called is True
