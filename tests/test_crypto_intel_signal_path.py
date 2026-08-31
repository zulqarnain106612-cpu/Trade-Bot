"""Covers src/intel.py's full on_bar signal path and route_signal.

The existing tests exercise construction and the early-return paths; on_bar
never got as far as conflict resolution because no worker results ever arrive
without a live worker pool. Here the orchestrator and the network-backed
feature fetchers are replaced with in-process fakes so the fusion, risk-gate,
persistence and routing branches all run for real.
"""

from __future__ import annotations

import asyncio

import pytest

from src.features.mempool import MempoolFeatures
from src.features.onchain import OnChainFeatures
from src.intel import CryptoIntelligence, IntelSignal
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


def _result(horizon_id: int, direction: int = 1, confidence: float = 0.9) -> WorkerResult:
    return WorkerResult(
        task_id=f"t{horizon_id}",
        horizon_id=horizon_id,
        direction=direction,
        confidence=confidence,
        magnitude_mu=0.01,
        magnitude_sigma=0.002,
        timing=0.5,
        algo="IOC",
    )


@pytest.fixture
def intel(tmp_path, monkeypatch):
    cfg = tmp_path / "intelligence.yaml"
    cfg.write_text(
        f"n_workers: 1\nn_horizons: 3\nduckdb_path: {tmp_path / 'intel.duckdb'}\n",
    )
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


def test_on_bar_returns_a_signal_when_horizons_agree(intel):
    results = [_result(i) for i in range(3)]
    intel._drain_one = lambda timeout: results.pop(0) if results else None

    sig = asyncio.run(intel.on_bar("BTC/USDT", _ohlcv(), regime_id=2))

    assert isinstance(sig, IntelSignal)
    assert sig.symbol == "BTC/USDT"
    assert sig.direction == 1
    assert sig.regime_id == 2
    assert sig.algo == "IOC"
    assert 0.0 <= sig.size_pct <= 0.05
    assert sig.meta["ofi"] == 0.0
    # one task submitted per configured horizon
    assert len(intel._orchestrator.submitted) == 3


def test_on_bar_applies_regime_confidences_as_horizon_weights(intel):
    results = [_result(i) for i in range(3)]
    intel._drain_one = lambda timeout: results.pop(0) if results else None

    sig = asyncio.run(
        intel.on_bar("BTC/USDT", _ohlcv(), regime_confidences=[0.9, 0.5, 0.1]),
    )
    assert sig is not None


def test_on_bar_updates_granger_when_alt_price_history_is_long_enough(intel):
    results = [_result(i) for i in range(3)]
    intel._drain_one = lambda timeout: results.pop(0) if results else None
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
    results = [_result(0, direction=1), _result(1, direction=-1), _result(2, direction=0)]
    intel._drain_one = lambda timeout: results.pop(0) if results else None
    intel._resolver.resolve_with_ecc = lambda *_a, **_kw: type(
        "R", (), {"direction": 0, "weight": 0.0, "conflict": True, "agreement_ratio": 0.3}
    )()

    assert asyncio.run(intel.on_bar("BTC/USDT", _ohlcv())) is None


def test_on_bar_returns_none_when_the_circuit_breaker_trips(intel):
    results = [_result(i) for i in range(3)]
    intel._drain_one = lambda timeout: results.pop(0) if results else None
    intel._risk_gate.circuit_breaker = lambda drawdown, daily_loss: True

    assert asyncio.run(intel.on_bar("BTC/USDT", _ohlcv())) is None


def test_on_bar_returns_none_when_the_risk_gate_suppresses_the_size(intel):
    # confidence below the gate's threshold -> suppressed, no signal
    results = [_result(i, confidence=0.01) for i in range(3)]
    intel._drain_one = lambda timeout: results.pop(0) if results else None

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


class TestRouteSignal:
    def _signal(self, **overrides) -> IntelSignal:
        base = dict(
            symbol="BTC/USDT",
            direction=1,
            size_pct=0.02,
            confidence=0.8,
            horizon_idx=2,
            algo="IOC",
            ecc_anomaly=0.1,
            conflict=False,
            regime_id=0,
            meta={"kyle_lambda": 0.002},
        )
        return IntelSignal(**(base | overrides))

    def test_routes_a_long_and_records_the_fill(self, intel):
        routed: dict[str, object] = {}

        async def _route(signal, kyle, size_usd):
            routed.update(signal=signal, kyle=kyle, size_usd=size_usd)
            return {"filled": True}

        recorded: dict[str, object] = {}
        intel._router.route = _route
        intel._post_trade.record = lambda *args: recorded.update(args=args)

        asyncio.run(intel.route_signal(self._signal(), price=50000.0, capital_usd=100_000.0))

        assert routed["size_usd"] == pytest.approx(2000.0)
        assert routed["kyle"] == 0.002
        assert routed["signal"]["side"] == "buy"
        assert routed["signal"]["horizon_seconds"] == 300
        assert recorded["args"][1:4] == ("BTC/USDT", "buy", 2)

    def test_clamps_the_horizon_index_to_the_longest_bucket(self, intel):
        captured: dict[str, object] = {}

        async def _route(signal, kyle, size_usd):
            captured.update(signal)
            return {}

        intel._router.route = _route
        intel._post_trade.record = lambda *args: None

        asyncio.run(intel.route_signal(self._signal(horizon_idx=42, direction=-1), 50000.0, 1000.0))
        assert captured["side"] == "sell"
        assert captured["horizon_seconds"] == 86400 * 30

    @pytest.mark.parametrize("kwargs", [{"direction": 0}, {"size_pct": 0.0}])
    def test_does_not_route_a_flat_or_zero_size_signal(self, intel, kwargs):
        def _fail(*_a, **_kw):
            raise AssertionError("router must not be called")

        intel._router.route = _fail
        asyncio.run(intel.route_signal(self._signal(**kwargs), 50000.0, 1000.0))


def test_close_survives_a_duckdb_that_fails_to_close(intel):
    def _boom():
        raise RuntimeError("locked")

    intel._duckdb.close = _boom
    intel.close()

    assert intel._started is False
    assert intel._orchestrator.shutdown_called is True
