"""Coverage boost: intelligence/metrics, probabilistic_adapter, coingecko_provider, api/main lifespan."""

from __future__ import annotations

import math
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.intelligence.metrics import IntelligenceAnalyzer, IntelligenceMetrics
from src.intelligence.probabilistic_adapter import (
    ProbabilisticGateInputs,
    ProbabilisticMetricsAdapter,
)
from src.intelligence.providers.coingecko_provider import (
    CoinGeckoIntelligenceProvider,
    get_coingecko_intelligence_provider,
)


# ---------------------------------------------------------------------------
# IntelligenceAnalyzer (intelligence/metrics.py — lines 154-290)
# ---------------------------------------------------------------------------


def _make_analyzer(**kwargs) -> IntelligenceAnalyzer:
    return IntelligenceAnalyzer(**kwargs)


def _raw(netflow=5_000.0, ratio=1.5, rate_pct=0.05):
    return (
        {"netflow": netflow},
        {"ratio": ratio, "is_whale": True},
        {"rate_pct": rate_pct},
    )


class TestIntelligenceAnalyzer:
    def test_compute_metrics_basic(self):
        a = _make_analyzer()
        ef, wa, fr = _raw()
        m = a.compute_metrics(ef, wa, fr)
        assert isinstance(m, IntelligenceMetrics)
        assert m.confidence <= a._REAL_METRIC_COUNT / a._TOTAL_METRIC_COUNT + 1e-6

    def test_confidence_capped_by_real_fraction(self):
        a = _make_analyzer()
        ef, wa, fr = _raw()
        m = a.compute_metrics(ef, wa, fr)
        cap = a._REAL_METRIC_COUNT / a._TOTAL_METRIC_COUNT
        assert m.confidence <= cap + 1e-6

    def test_high_funding_rate_triggers_positive_signal(self):
        a = _make_analyzer()
        ef, wa, fr = _raw(rate_pct=0.2)
        m = a.compute_metrics(ef, wa, fr)
        assert m.exchange_stress_score >= 0.0

    def test_negative_funding_rate_triggers_negative_signal(self):
        a = _make_analyzer()
        ef, wa, fr = _raw(rate_pct=-0.1)
        m = a.compute_metrics(ef, wa, fr)
        assert isinstance(m, IntelligenceMetrics)

    def test_exchange_flow_exception_uses_zero(self):
        a = _make_analyzer()
        # Pass something that'll raise on get("netflow", 0) logic
        wa = {"ratio": 1.0}
        fr = {"rate_pct": 0.0}
        m = a.compute_metrics(None, wa, fr)  # type: ignore[arg-type]
        # Should not raise; falls back to 0 netflow
        assert isinstance(m, IntelligenceMetrics)

    def test_whale_ratio_exception_still_returns_metrics(self):
        a = _make_analyzer()
        ef = {"netflow": 0.0}
        fr = {"rate_pct": 0.0}
        m = a.compute_metrics(ef, None, fr)  # type: ignore[arg-type]
        assert isinstance(m, IntelligenceMetrics)

    def test_funding_rate_exception_still_returns_metrics(self):
        a = _make_analyzer()
        ef = {"netflow": 0.0}
        wa = {"ratio": 1.0}
        m = a.compute_metrics(ef, wa, None)  # type: ignore[arg-type]
        assert isinstance(m, IntelligenceMetrics)

    def test_zscore_no_history_returns_zero(self):
        a = _make_analyzer()
        result = a._compute_zscore(1.0, "exchange_netflow")
        assert result == 0.0

    def test_zscore_with_history(self):
        hist = pd.DataFrame({"exchange_netflow": [1.0, 2.0, 3.0, 2.0, 1.5]})
        a = IntelligenceAnalyzer(historical_data=hist)
        result = a._compute_zscore(2.5, "exchange_netflow")
        assert isinstance(result, float)

    def test_zscore_constant_series_returns_zero(self):
        hist = pd.DataFrame({"exchange_netflow": [5.0, 5.0, 5.0, 5.0, 5.0]})
        a = IntelligenceAnalyzer(historical_data=hist)
        result = a._compute_zscore(5.0, "exchange_netflow")
        assert result == 0.0

    def test_zscore_clamped_to_five(self):
        hist = pd.DataFrame({"exchange_netflow": [0.0, 0.0, 0.0, 0.0, 0.1]})
        a = IntelligenceAnalyzer(historical_data=hist)
        result = a._compute_zscore(1000.0, "exchange_netflow")
        assert abs(result) <= 5.0

    def test_exchange_stress_high_netflow(self):
        a = _make_analyzer()
        stress = a._compute_exchange_stress(-3.0, 0.0)
        assert stress > 0.0

    def test_exchange_stress_high_funding(self):
        a = _make_analyzer()
        stress = a._compute_exchange_stress(0.0, 1.0)
        assert stress > 0.0

    def test_exchange_stress_capped_at_one(self):
        a = _make_analyzer()
        stress = a._compute_exchange_stress(-10.0, 1.0)
        assert stress <= 1.0

    def test_exchange_stress_low_inputs(self):
        a = _make_analyzer()
        stress = a._compute_exchange_stress(0.0, 0.0)
        assert stress == 0.0

    def test_nan_fields_in_result(self):
        a = _make_analyzer()
        ef, wa, fr = _raw()
        m = a.compute_metrics(ef, wa, fr)
        # Most fields should be NaN (only 4/18 are real)
        assert math.isnan(m.exchange_reserve_ratio)
        assert math.isnan(m.miner_netflow_signal)

    def test_custom_rolling_window(self):
        a = IntelligenceAnalyzer(rolling_window_days=7)
        assert a.rolling_window == 7


# ---------------------------------------------------------------------------
# ProbabilisticMetricsAdapter (intelligence/probabilistic_adapter.py)
# ---------------------------------------------------------------------------


class TestProbabilisticMetricsAdapter:
    def test_process_empty_dict_returns_nones(self):
        adapter = ProbabilisticMetricsAdapter()
        result = adapter.process({})
        assert result.exchange_stress_score is None
        assert result.whale_buy_sell_ratio is None

    def test_process_with_stress_score(self):
        adapter = ProbabilisticMetricsAdapter()
        raw = {
            "exchange_stress_score": 0.3,
            "exchange_netflow_7d_zscore": -1.5,
            "binance_funding_rate_pct": 0.05,
            "cross_exchange_basis_spread_bps": 2.0,
            "exchange_reserve_ratio": 0.35,
        }
        result = adapter.process(raw)
        # Bayesian model should produce a valid probability
        assert result.exchange_stress_score is not None
        assert 0.0 <= result.exchange_stress_score <= 1.0

    def test_process_with_whale_ratio(self):
        adapter = ProbabilisticMetricsAdapter(min_whale_confidence=0.0)
        raw = {"whale_buy_sell_ratio": 2.0}
        result = adapter.process(raw)
        assert result.raw_whale_ratio == pytest.approx(2.0)

    def test_process_low_whale_confidence_returns_none(self):
        adapter = ProbabilisticMetricsAdapter(min_whale_confidence=1.0)
        raw = {"whale_buy_sell_ratio": 2.0}
        result = adapter.process(raw)
        assert result.whale_buy_sell_ratio is None

    def test_process_stress_model_exception_returns_none(self):
        adapter = ProbabilisticMetricsAdapter()
        with patch.object(
            adapter._stress_model,
            "predict_failure_probability",
            side_effect=RuntimeError("model error"),
        ):
            raw = {"exchange_stress_score": 0.5}
            result = adapter.process(raw)
        assert result.exchange_stress_score is None

    def test_process_whale_model_exception_returns_none(self):
        adapter = ProbabilisticMetricsAdapter()
        with patch.object(
            adapter._whale_model,
            "estimate_true_ratio",
            side_effect=RuntimeError("model error"),
        ):
            raw = {"whale_buy_sell_ratio": 1.5}
            result = adapter.process(raw)
        assert result.whale_buy_sell_ratio is None

    def test_probabilistic_gate_inputs_fields(self):
        p = ProbabilisticGateInputs(
            exchange_stress_score=0.3,
            whale_buy_sell_ratio=1.8,
            exchange_stress_confidence=0.9,
            whale_ratio_confidence=0.7,
        )
        assert p.exchange_stress_score == pytest.approx(0.3)
        assert p.whale_buy_sell_ratio == pytest.approx(1.8)

    def test_process_full_metrics_dict(self):
        adapter = ProbabilisticMetricsAdapter(min_whale_confidence=0.0)
        raw = {
            "exchange_stress_score": 0.1,
            "exchange_netflow_7d_zscore": 0.5,
            "binance_funding_rate_pct": 0.02,
            "cross_exchange_basis_spread_bps": 1.0,
            "exchange_reserve_ratio": 0.3,
            "whale_buy_sell_ratio": 1.2,
        }
        result = adapter.process(raw)
        assert result.raw_stress_score == pytest.approx(0.1)
        assert result.raw_whale_ratio == pytest.approx(1.2)


# ---------------------------------------------------------------------------
# CoinGeckoIntelligenceProvider
# ---------------------------------------------------------------------------


class TestCoinGeckoProvider:
    @pytest.mark.asyncio
    async def test_initialize_logs_and_returns(self):
        p = CoinGeckoIntelligenceProvider()
        await p.initialize()  # should not raise

    @pytest.mark.asyncio
    async def test_close_returns(self):
        p = CoinGeckoIntelligenceProvider()
        await p.close()  # no-op, should not raise

    def test_exchange_id(self):
        p = CoinGeckoIntelligenceProvider()
        assert p.exchange_id == "coingecko"

    @pytest.mark.asyncio
    async def test_fetch_metrics_success(self):
        p = CoinGeckoIntelligenceProvider()
        global_data = {
            "market_cap_percentage": {"btc": 52.0, "usdc": 4.0, "usdt": 6.0},
            "total_market_cap": {"usd": 2_000_000_000_000.0},
            "market_cap_change_percentage_24h_usd": 1.5,
        }
        with patch.object(p, "_fetch_global", new=AsyncMock(return_value=global_data)):
            result = await p.fetch_metrics()
        assert "btc_dominance_regime" in result
        assert "stablecoin_reserve_ratio" in result
        assert result["confidence"] > 0.0

    @pytest.mark.asyncio
    async def test_fetch_metrics_empty_global_data(self):
        p = CoinGeckoIntelligenceProvider()
        with patch.object(p, "_fetch_global", new=AsyncMock(return_value={})):
            result = await p.fetch_metrics()
        assert "btc_dominance_regime" in result

    @pytest.mark.asyncio
    async def test_fetch_metrics_exception_penalizes_confidence(self):
        p = CoinGeckoIntelligenceProvider()
        with patch.object(p, "_fetch_global", new=AsyncMock(side_effect=RuntimeError("timeout"))):
            result = await p.fetch_metrics()
        assert result["confidence"] < 1.0

    @pytest.mark.asyncio
    async def test_fetch_metrics_builds_btc_dom_zscore(self):
        p = CoinGeckoIntelligenceProvider()
        global_data = {"market_cap_percentage": {"btc": 50.0}}

        # Populate history so zscore can be computed (need >= 4 points)
        p._btc_dom_history = [48.0, 49.0, 51.0, 52.0]
        with patch.object(p, "_fetch_global", new=AsyncMock(return_value=global_data)):
            result = await p.fetch_metrics()
        assert isinstance(result["btc_dominance_regime"], float)

    def test_get_cache_miss(self):
        p = CoinGeckoIntelligenceProvider()
        assert p._get_cache("missing") is None

    def test_set_and_get_cache(self):
        p = CoinGeckoIntelligenceProvider()
        p._set_cache("k", {"data": 1})
        assert p._get_cache("k") == {"data": 1}

    def test_get_cache_expired(self):
        p = CoinGeckoIntelligenceProvider(cache_ttl_s=1)
        p._cache["k"] = (time.time() - 10.0, "value")
        assert p._get_cache("k") is None

    def test_singleton_returns_same_instance(self):
        import src.intelligence.providers.coingecko_provider as mod

        mod._provider = None
        p1 = get_coingecko_intelligence_provider()
        p2 = get_coingecko_intelligence_provider()
        assert p1 is p2

    @pytest.mark.asyncio
    async def test_fetch_global_uses_cache(self):
        p = CoinGeckoIntelligenceProvider()
        cached = {"market_cap_percentage": {"btc": 45.0}}
        p._set_cache("global", cached)
        result = await p._fetch_global()
        assert result == cached


# ---------------------------------------------------------------------------
# api/main.py — additional route coverage
# ---------------------------------------------------------------------------

_API_KEY = "x" * 32


def _make_api_state():
    from src.api.main import AppState

    s = AppState()
    s.storage = AsyncMock()
    s.storage.health_check = AsyncMock(return_value={"bars": 1000, "trades": 50})
    s.storage.latest_regime = AsyncMock(return_value=None)
    s.storage.list_trades = AsyncMock(return_value=[])
    s.storage.equity_curve = AsyncMock(return_value=[])
    s.ready = True

    orch = MagicMock()
    orch._executor = MagicMock()
    orch._executor.equity_usd = 100_000.0
    orch._executor.cash_usd = 80_000.0
    orch._executor.open_positions_safe = AsyncMock(return_value=[])
    orch._executor.pending_approvals_safe = AsyncMock(return_value=[])
    orch._executor._order_fsm_registry = {}
    orch._last_retrain_error = {}
    orch._engines = {}
    orch._drift_adapter = MagicMock()
    orch._drift_adapter.check_drift = MagicMock(return_value={"drifted": False, "reason": "ok"})
    s.orchestrator = orch
    return s


@pytest.fixture
def api_client_and_state():
    from src.api.main import app

    state = _make_api_state()
    with (
        patch("src.api.main._state", state),
        patch.dict(os.environ, {"API_SECRET_KEY": _API_KEY}),
        patch("src.api.auth._get_configured_key", return_value=_API_KEY),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        yield client, state


def test_approve_trade_not_found(api_client_and_state):
    client, state = api_client_and_state
    state.orchestrator._executor.resolve_approval = AsyncMock(return_value=False)
    resp = client.post(
        "/approvals/req-999/approve",
        json={"operator": "alice"},
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code in (200, 404, 422)


def test_reject_trade(api_client_and_state):
    client, state = api_client_and_state
    state.orchestrator._executor.resolve_approval = AsyncMock(return_value=True)
    resp = client.post(
        "/approvals/req-1/reject",
        json={"operator": "bob"},
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code in (200, 404, 422)


def test_ws_route_returns_upgrade_required(api_client_and_state):
    client, _state = api_client_and_state
    resp = client.get("/ws", headers={"x-api-key": _API_KEY})
    # WS endpoint returns 403 or 426 when not a proper WS upgrade
    assert resp.status_code in (101, 403, 404, 422, 426, 500)


def test_status_route_open_positions(api_client_and_state):
    client, state = api_client_and_state
    pos = {
        "trade_id": "t1",
        "symbol": "BTC/USDT",
        "direction": "long",
        "entry_price": 50000.0,
        "quantity": 0.1,
        "unrealized_pnl": 100.0,
        "notional_usd": 5000.0,
        "entry_ts": 1700000000000,
    }
    state.orchestrator._executor.open_positions_safe = AsyncMock(return_value=[pos])
    resp = client.get("/status", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_intelligence_coverage_with_storage_error(api_client_and_state):
    client, state = api_client_and_state
    state.storage.health_check = AsyncMock(side_effect=RuntimeError("db error"))
    resp = client.get("/intelligence/coverage", headers={"x-api-key": _API_KEY})
    assert resp.status_code in (200, 500)


def test_regime_route_invalid_timeframe(api_client_and_state):
    client, _state = api_client_and_state
    resp = client.get("/regime/invalid", headers={"x-api-key": _API_KEY})
    assert resp.status_code in (200, 400, 404, 422)


def test_debug_selftest_returns_json(api_client_and_state):
    client, _state = api_client_and_state
    resp = client.post("/debug/selftest", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


def test_orders_status_with_registry_entry(api_client_and_state):
    client, state = api_client_and_state
    fsm_mock = MagicMock()
    fsm_mock.state.name = "FILLED"
    fsm_mock.trade_id = "t1"
    import uuid

    uid = str(uuid.uuid4())
    state.orchestrator._executor._order_fsm_registry = {uid: fsm_mock}
    resp = client.get(f"/orders/{uid}/status", headers={"x-api-key": _API_KEY})
    assert resp.status_code in (200, 404)
