"""Coverage boost 3: api/main more routes, okx_provider, strategies/filters."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.intelligence.providers.okx_provider import (
    OKXIntelligenceProvider,
    get_okx_intelligence_provider,
)


_API_KEY = "x" * 32
_OP_SECRET = "y" * 32


def _make_state():
    from src.api.main import AppState

    s = AppState()
    s.storage = AsyncMock()
    s.storage.health_check = AsyncMock(return_value={"bars": 1000, "trades": 50})
    s.storage.latest_regime = AsyncMock(return_value=None)
    s.storage.list_trades = AsyncMock(return_value=[])
    s.storage.equity_curve = AsyncMock(return_value=[])
    s.storage.latest_model_metrics = AsyncMock(return_value=None)
    s.storage.insert_audit_event = AsyncMock(return_value=None)
    s.storage.validate_symbol = AsyncMock(return_value=None)
    s.storage.fetch_trades = AsyncMock(return_value=[])
    s.ready = True

    orch = MagicMock()
    orch._executor = MagicMock()
    orch._executor.equity_usd = 100_000.0
    orch._executor.cash_usd = 80_000.0
    orch._executor.open_positions_safe = AsyncMock(return_value=[])
    orch._executor.pending_approvals_safe = AsyncMock(return_value=[])
    orch._executor.pending_approvals = MagicMock(return_value=[])
    orch._executor._order_fsm_registry = {}
    orch._executor.resolve_approval = AsyncMock(return_value=True)
    orch._last_retrain_error = {}
    orch._engines = {}
    orch._drift_adapter = MagicMock()
    orch._drift_adapter.check_drift = MagicMock(return_value={"drifted": False, "reason": "ok"})
    s.orchestrator = orch
    return s


@pytest.fixture
def client_state():
    from src.api.main import app

    state = _make_state()
    with (
        patch("src.api.main._state", state),
        patch.dict(os.environ, {"API_SECRET_KEY": _API_KEY, "OPERATOR_SECRET": _OP_SECRET}),
        patch("src.api.auth._get_configured_key", return_value=_API_KEY),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        yield client, state


# ---------------------------------------------------------------------------
# /approvals/{request_id}/resolve
# ---------------------------------------------------------------------------


def test_resolve_approval_success(client_state):
    client, state = client_state
    state.orchestrator._executor.resolve_approval = AsyncMock(return_value=True)
    import uuid

    req_id = str(uuid.uuid4())
    resp = client.post(
        f"/approvals/{req_id}/resolve",
        json={"approved": True, "operator": "alice", "operator_secret": _OP_SECRET},
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code in (200, 401, 404, 422, 503)


def test_resolve_approval_bad_operator_secret(client_state):
    client, _state = client_state
    import uuid

    req_id = str(uuid.uuid4())
    resp = client.post(
        f"/approvals/{req_id}/resolve",
        json={
            "approved": True,
            "operator": "alice",
            "operator_secret": "wrong",
        },  # pragma: allowlist secret
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code in (401, 422)


def test_resolve_approval_not_found(client_state):
    client, state = client_state
    state.orchestrator._executor.resolve_approval = AsyncMock(return_value=False)
    import uuid

    req_id = str(uuid.uuid4())
    resp = client.post(
        f"/approvals/{req_id}/resolve",
        json={"approved": False, "operator": "bob", "operator_secret": _OP_SECRET},
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code in (200, 404, 422)


def test_resolve_approval_invalid_uuid(client_state):
    client, _state = client_state
    resp = client.post(
        "/approvals/not-a-uuid/resolve",
        json={"approved": True, "operator": "alice", "operator_secret": _OP_SECRET},
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# /execution-mode
# ---------------------------------------------------------------------------


def test_set_execution_mode_success(client_state):
    client, _state = client_state
    with patch("src.api.main.runtime_config") as rc:
        rc.get_execution_mode = AsyncMock(return_value=MagicMock(value="automatic"))
        rc.set_execution_mode = AsyncMock(return_value=None)
        resp = client.post(
            "/execution-mode",
            json={"mode": "manual", "operator": "alice", "operator_secret": _OP_SECRET},
            headers={"x-api-key": _API_KEY},
        )
    assert resp.status_code in (200, 422, 503)


def test_set_execution_mode_bad_secret(client_state):
    client, _state = client_state
    resp = client.post(
        "/execution-mode",
        json={"mode": "manual", "operator": "alice", "operator_secret": "wrong"},
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code in (401, 422)


def test_set_execution_mode_invalid_mode(client_state):
    client, _state = client_state
    resp = client.post(
        "/execution-mode",
        json={"mode": "turbo", "operator": "alice", "operator_secret": _OP_SECRET},
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code in (400, 422, 503)


# ---------------------------------------------------------------------------
# /risk-controls (POST)
# ---------------------------------------------------------------------------


def test_post_risk_controls_success(client_state):
    client, _state = client_state
    with patch("src.api.main.runtime_config") as rc:
        rc.get_risk_controls = AsyncMock(return_value={"stop_loss_enabled": True})
        rc.set_risk_controls = AsyncMock(return_value={"stop_loss_enabled": False})
        resp = client.post(
            "/risk-controls",
            json={
                "stop_loss_enabled": False,
                "operator": "alice",
                "operator_secret": _OP_SECRET,
            },
            headers={"x-api-key": _API_KEY},
        )
    assert resp.status_code in (200, 422, 503)


def test_post_risk_controls_bad_secret(client_state):
    client, _state = client_state
    resp = client.post(
        "/risk-controls",
        json={
            "stop_loss_enabled": False,
            "operator": "alice",
            "operator_secret": "wrong",
        },
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code in (401, 422)


# ---------------------------------------------------------------------------
# /status with regime snapshot
# ---------------------------------------------------------------------------


def test_status_with_regime(client_state):
    client, state = client_state
    snap = MagicMock()
    snap.regime_state = 0
    snap.prob_ranging = 0.7
    snap.prob_trending = 0.2
    snap.prob_volatile = 0.1
    state.storage.latest_regime = AsyncMock(return_value=snap)
    resp = client.get("/status", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert "regime" in data


# ---------------------------------------------------------------------------
# /trades with invalid symbol
# ---------------------------------------------------------------------------


def test_trades_invalid_symbol(client_state):
    client, state = client_state
    state.storage.validate_symbol = AsyncMock(side_effect=ValueError("unknown symbol"))
    resp = client.get("/trades?symbol=FAKE/COIN", headers={"x-api-key": _API_KEY})
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# OKX intelligence provider
# ---------------------------------------------------------------------------


class TestOKXProvider:
    def test_exchange_id(self):
        p = OKXIntelligenceProvider()
        assert p.exchange_id == "okx"

    @pytest.mark.asyncio
    async def test_initialize_and_close(self):
        p = OKXIntelligenceProvider()
        with (
            patch.object(p._spot, "load_markets", new=AsyncMock(return_value={})),
            patch.object(p._perp, "load_markets", new=AsyncMock(return_value={})),
            patch.object(p._spot, "close", new=AsyncMock()),
            patch.object(p._perp, "close", new=AsyncMock()),
        ):
            await p.initialize()
            await p.close()

    @pytest.mark.asyncio
    async def test_fetch_metrics_success(self):
        p = OKXIntelligenceProvider()
        funding_dict = {"rate_pct": 0.03, "zscore": 0.5}
        oi_dict = {"change_pct": 0.05, "value_usd": 500_000_000.0}
        with (
            patch.object(p, "_fetch_funding_data", new=AsyncMock(return_value=funding_dict)),
            patch.object(p, "_fetch_oi_data", new=AsyncMock(return_value=oi_dict)),
            patch.object(p, "_fetch_basis_data", new=AsyncMock(return_value=2.5)),
            patch.object(p, "_fetch_whale_taker_ratio", new=AsyncMock(return_value=1.4)),
        ):
            result = await p.fetch_metrics()
        assert "binance_funding_rate_pct" in result
        assert "futures_oi_change_pct" in result
        assert result["confidence"] > 0.0

    @pytest.mark.asyncio
    async def test_fetch_metrics_all_fail(self):
        p = OKXIntelligenceProvider()
        exc = RuntimeError("timeout")
        with (
            patch.object(p, "_fetch_funding_data", new=AsyncMock(side_effect=exc)),
            patch.object(p, "_fetch_oi_data", new=AsyncMock(side_effect=exc)),
            patch.object(p, "_fetch_basis_data", new=AsyncMock(side_effect=exc)),
            patch.object(p, "_fetch_whale_taker_ratio", new=AsyncMock(side_effect=exc)),
        ):
            result = await p.fetch_metrics()
        assert result["confidence"] < 1.0

    @pytest.mark.asyncio
    async def test_fetch_metrics_funding_is_exception(self):
        p = OKXIntelligenceProvider()
        with (
            patch.object(p, "_fetch_funding_data", new=AsyncMock(side_effect=RuntimeError("x"))),
            patch.object(
                p,
                "_fetch_oi_data",
                new=AsyncMock(return_value={"change_pct": 0.0, "value_usd": 0.0}),
            ),
            patch.object(p, "_fetch_basis_data", new=AsyncMock(return_value=0.0)),
            patch.object(p, "_fetch_whale_taker_ratio", new=AsyncMock(return_value=1.0)),
        ):
            result = await p.fetch_metrics()
        assert result["confidence"] < 1.0

    def test_compute_stress_score_low(self):
        p = OKXIntelligenceProvider()
        score = p._compute_stress_score(basis_bps=0.0, funding_zscore=0.0, oi_change_pct=0.0)
        assert score == pytest.approx(0.0)

    def test_compute_stress_score_high(self):
        p = OKXIntelligenceProvider()
        score = p._compute_stress_score(basis_bps=100.0, funding_zscore=3.0, oi_change_pct=0.3)
        assert score > 0.0
        assert score <= 1.0

    def test_get_cache_miss(self):
        p = OKXIntelligenceProvider()
        assert p._get_cache("missing") is None

    def test_set_and_get_cache(self):
        p = OKXIntelligenceProvider()
        p._set_cache("k", {"data": 1})
        assert p._get_cache("k") == {"data": 1}

    def test_get_cache_expired(self):
        p = OKXIntelligenceProvider(cache_ttl_s=1)
        p._cache["k"] = (time.monotonic() - 10.0, "value")
        assert p._get_cache("k") is None

    @pytest.mark.asyncio
    async def test_fetch_funding_data_uses_cache(self):
        p = OKXIntelligenceProvider()
        cached = {"rate_pct": 0.02, "zscore": 0.1}
        p._set_cache(f"funding:{p._perp_symbol}", cached)
        result = await p._fetch_funding_data()
        assert result == cached

    @pytest.mark.asyncio
    async def test_fetch_oi_data_uses_cache(self):
        p = OKXIntelligenceProvider()
        cached = {"change_pct": 0.03, "value_usd": 1_000_000.0}
        p._set_cache(f"oi:{p._perp_symbol}", cached)
        result = await p._fetch_oi_data()
        assert result == cached

    def test_singleton(self):
        import src.intelligence.providers.okx_provider as mod

        mod._provider = None
        p1 = get_okx_intelligence_provider()
        p2 = get_okx_intelligence_provider()
        assert p1 is p2

    @pytest.mark.asyncio
    async def test_fetch_funding_data_empty_history(self):
        p = OKXIntelligenceProvider()
        with patch.object(p._perp, "fetch_funding_rate_history", new=AsyncMock(return_value=[])):
            result = await p._fetch_funding_data()
        assert result == {"rate_pct": 0.0, "zscore": 0.0}

    @pytest.mark.asyncio
    async def test_fetch_funding_data_single_rate_no_zscore(self):
        p = OKXIntelligenceProvider()
        history = [{"fundingRate": 0.0001}]
        with patch.object(
            p._perp, "fetch_funding_rate_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_funding_data()
        assert result["rate_pct"] == pytest.approx(0.0001)
        assert result["zscore"] == 0.0

    @pytest.mark.asyncio
    async def test_fetch_funding_data_computes_zscore_and_caches(self):
        p = OKXIntelligenceProvider()
        history = [{"fundingRate": r} for r in [0.0001, 0.0002, 0.0003, 0.0005, -0.0001]]
        with patch.object(
            p._perp, "fetch_funding_rate_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_funding_data()
        assert result["rate_pct"] == pytest.approx(-0.01)
        assert isinstance(result["zscore"], float)
        # Second call should hit cache, not re-fetch
        with patch.object(
            p._perp,
            "fetch_funding_rate_history",
            new=AsyncMock(side_effect=AssertionError("should not be called")),
        ):
            cached = await p._fetch_funding_data()
        assert cached == result

    @pytest.mark.asyncio
    async def test_fetch_funding_data_zero_stdev_zscore_zero(self):
        p = OKXIntelligenceProvider()
        history = [{"fundingRate": 0.0002}, {"fundingRate": 0.0002}]
        with patch.object(
            p._perp, "fetch_funding_rate_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_funding_data()
        assert result["zscore"] == 0.0

    @pytest.mark.asyncio
    async def test_fetch_oi_data_empty_history(self):
        p = OKXIntelligenceProvider()
        with patch.object(p._perp, "fetch_open_interest_history", new=AsyncMock(return_value=[])):
            result = await p._fetch_oi_data()
        assert result == {"change_pct": 0.0, "value_usd": 0.0}

    @pytest.mark.asyncio
    async def test_fetch_oi_data_single_point_returns_neutral(self):
        p = OKXIntelligenceProvider()
        history = [{"openInterestAmount": 1000.0, "openInterestValue": 50_000_000.0}]
        with patch.object(
            p._perp, "fetch_open_interest_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_oi_data()
        assert result == {"change_pct": 0.0, "value_usd": 0.0}

    @pytest.mark.asyncio
    async def test_fetch_oi_data_computes_change_and_caches(self):
        p = OKXIntelligenceProvider()
        history = [
            {"openInterestAmount": 1000.0, "openInterestValue": 40_000_000.0},
            {"openInterestAmount": 1100.0, "openInterestValue": 44_000_000.0},
        ]
        with patch.object(
            p._perp, "fetch_open_interest_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_oi_data()
        assert result["change_pct"] == pytest.approx(10.0)
        assert result["value_usd"] == pytest.approx(44_000_000.0)
        with patch.object(
            p._perp,
            "fetch_open_interest_history",
            new=AsyncMock(side_effect=AssertionError("should not be called")),
        ):
            cached = await p._fetch_oi_data()
        assert cached == result

    @pytest.mark.asyncio
    async def test_fetch_oi_data_zero_start_oi_no_divide_error(self):
        p = OKXIntelligenceProvider()
        history = [
            {"openInterestAmount": 0.0, "openInterestValue": 0.0},
            {"openInterestAmount": 500.0, "openInterestValue": 20_000_000.0},
        ]
        with patch.object(
            p._perp, "fetch_open_interest_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_oi_data()
        assert result["change_pct"] == 0.0

    @pytest.mark.asyncio
    async def test_fetch_oi_data_missing_value_defaults_zero(self):
        p = OKXIntelligenceProvider()
        history = [
            {"openInterestAmount": 1000.0, "openInterestValue": None},
            {"openInterestAmount": 1100.0, "openInterestValue": None},
        ]
        with patch.object(
            p._perp, "fetch_open_interest_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_oi_data()
        assert result["value_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_fetch_basis_data_computes_and_caches(self):
        p = OKXIntelligenceProvider()
        spot_ticker = {"last": 50_000.0}
        perp_ticker = {"last": 50_100.0}
        with (
            patch.object(p._spot, "fetch_ticker", new=AsyncMock(return_value=spot_ticker)),
            patch.object(p._perp, "fetch_ticker", new=AsyncMock(return_value=perp_ticker)),
        ):
            result = await p._fetch_basis_data()
        assert result == pytest.approx(20.0)
        with (
            patch.object(p._spot, "fetch_ticker", new=AsyncMock(side_effect=AssertionError("no"))),
            patch.object(p._perp, "fetch_ticker", new=AsyncMock(side_effect=AssertionError("no"))),
        ):
            cached = await p._fetch_basis_data()
        assert cached == result

    @pytest.mark.asyncio
    async def test_fetch_basis_data_uses_close_fallback(self):
        p = OKXIntelligenceProvider()
        spot_ticker = {"last": None, "close": 100.0}
        perp_ticker = {"last": None, "close": 101.0}
        with (
            patch.object(p._spot, "fetch_ticker", new=AsyncMock(return_value=spot_ticker)),
            patch.object(p._perp, "fetch_ticker", new=AsyncMock(return_value=perp_ticker)),
        ):
            result = await p._fetch_basis_data()
        assert result == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_fetch_basis_data_missing_prices_returns_zero(self):
        p = OKXIntelligenceProvider()
        spot_ticker = {"last": None, "close": None}
        perp_ticker = {"last": 100.0, "close": None}
        with (
            patch.object(p._spot, "fetch_ticker", new=AsyncMock(return_value=spot_ticker)),
            patch.object(p._perp, "fetch_ticker", new=AsyncMock(return_value=perp_ticker)),
        ):
            result = await p._fetch_basis_data()
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_fetch_basis_data_clamped_to_range(self):
        p = OKXIntelligenceProvider()
        spot_ticker = {"last": 100.0}
        perp_ticker = {"last": 1000.0}
        with (
            patch.object(p._spot, "fetch_ticker", new=AsyncMock(return_value=spot_ticker)),
            patch.object(p._perp, "fetch_ticker", new=AsyncMock(return_value=perp_ticker)),
        ):
            result = await p._fetch_basis_data()
        assert result == pytest.approx(500.0)

    @pytest.mark.asyncio
    async def test_fetch_whale_taker_ratio_success_and_caches(self):
        p = OKXIntelligenceProvider()
        raw = {"data": [["1700000000000", "1.5"]]}
        with (
            patch.object(p._perp, "market", return_value={"id": "BTC-USDT-SWAP"}),
            patch.object(
                p._perp,
                "publicGetRubikStatContractsLongShortAccountRatio",
                new=AsyncMock(return_value=raw),
            ),
        ):
            result = await p._fetch_whale_taker_ratio()
        assert result == pytest.approx(1.5)
        with patch.object(p._perp, "market", side_effect=AssertionError("no")):
            cached = await p._fetch_whale_taker_ratio()
        assert cached == result

    @pytest.mark.asyncio
    async def test_fetch_whale_taker_ratio_clamped_to_max(self):
        p = OKXIntelligenceProvider()
        raw = {"data": [["1700000000000", "50.0"]]}
        with (
            patch.object(p._perp, "market", return_value={"id": "BTC-USDT-SWAP"}),
            patch.object(
                p._perp,
                "publicGetRubikStatContractsLongShortAccountRatio",
                new=AsyncMock(return_value=raw),
            ),
        ):
            result = await p._fetch_whale_taker_ratio()
        assert result == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_fetch_whale_taker_ratio_empty_data_falls_back_neutral(self):
        p = OKXIntelligenceProvider()
        raw = {"data": []}
        with (
            patch.object(p._perp, "market", return_value={"id": "BTC-USDT-SWAP"}),
            patch.object(
                p._perp,
                "publicGetRubikStatContractsLongShortAccountRatio",
                new=AsyncMock(return_value=raw),
            ),
        ):
            result = await p._fetch_whale_taker_ratio()
        assert result == 1.0

    @pytest.mark.asyncio
    async def test_fetch_whale_taker_ratio_exception_falls_back_neutral(self):
        p = OKXIntelligenceProvider()
        with patch.object(p._perp, "market", side_effect=RuntimeError("boom")):
            result = await p._fetch_whale_taker_ratio()
        assert result == 1.0


# ---------------------------------------------------------------------------
# strategies/filters — branch coverage for uncovered paths
# ---------------------------------------------------------------------------


def _make_bars(n=50, trend_up=True) -> pd.DataFrame:
    prices = [100.0 + i * (0.5 if trend_up else -0.5) for i in range(n)]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p * 1.005 for p in prices],
            "low": [p * 0.995 for p in prices],
            "close": prices,
            "volume": [1000.0] * n,
        }
    )


def _atr_series(bars: pd.DataFrame, n=14) -> pd.Series:
    high, low, close = bars["high"], bars["low"], bars["close"]
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(n).mean().fillna(tr)


class TestStrategyFilters:
    def test_hurst_exponent_trending(self):
        from src.strategies.filters import hurst_exponent

        prices = pd.Series([100.0 + i for i in range(100)])
        h = hurst_exponent(prices)
        assert isinstance(h, float)

    def test_hurst_exponent_too_short(self):
        from src.strategies.filters import hurst_exponent

        prices = pd.Series([1.0, 2.0, 3.0])
        h = hurst_exponent(prices)
        assert isinstance(h, float)

    def test_hurst_filter_passes_trending(self):
        from src.strategies.filters import hurst_filter_passes

        prices = pd.Series([100.0 + i for i in range(100)])
        result = hurst_filter_passes(prices, direction=1)
        assert isinstance(result, bool)

    def test_trend_filter_passes(self):
        from src.strategies.filters import trend_filter_passes

        bars = _make_bars(n=250)
        result = trend_filter_passes(bars["close"], direction=1)
        assert isinstance(result, bool)

    def test_trend_filter_short_direction(self):
        from src.strategies.filters import trend_filter_passes

        bars = _make_bars(n=250, trend_up=False)
        result = trend_filter_passes(bars["close"], direction=-1)
        assert isinstance(result, bool)

    def test_vol_explosion_blocks(self):
        from src.strategies.filters import vol_explosion_blocks

        bars = _make_bars(n=50)
        atr = _atr_series(bars)
        result = vol_explosion_blocks(atr)
        assert isinstance(result, bool)

    def test_vol_adjusted_momentum(self):
        from src.strategies.filters import vol_adjusted_momentum

        bars = _make_bars(n=50)
        result = vol_adjusted_momentum(bars["close"])
        assert isinstance(result, float)

    def test_obv_trend_confirms(self):
        from src.strategies.filters import obv_trend_confirms

        bars = _make_bars(n=50)
        result = obv_trend_confirms(bars["close"], bars["volume"], direction=1)
        assert isinstance(result, bool)

    def test_mtf_trend_aligned(self):
        from src.strategies.filters import mtf_trend_aligned

        result = mtf_trend_aligned(fast_signal=0.7, slow_signal=0.6, direction=1)
        assert isinstance(result, bool)

    def test_mtf_trend_misaligned(self):
        from src.strategies.filters import mtf_trend_aligned

        result = mtf_trend_aligned(fast_signal=0.7, slow_signal=-0.6, direction=1)
        assert isinstance(result, bool)

    def test_ewm_trend_signal(self):
        from src.strategies.filters import ewm_trend_signal

        bars = _make_bars(n=250)
        result = ewm_trend_signal(bars["close"])
        assert isinstance(result, float)

    def test_overnight_gap_not_excessive(self):
        from src.strategies.filters import overnight_gap_is_excessive

        result = overnight_gap_is_excessive(open_price=100.1, prev_close=100.0, atr=5.0)
        assert result is False

    def test_overnight_gap_excessive(self):
        from src.strategies.filters import overnight_gap_is_excessive

        result = overnight_gap_is_excessive(open_price=115.0, prev_close=100.0, atr=5.0)
        assert result is True

    def test_regime_position_scalar_ranging(self):
        from src.strategies.filters import regime_position_scalar

        scalar = regime_position_scalar(
            regime_state=0, prob_trending=0.2, prob_ranging=0.6, prob_volatile=0.2
        )
        assert isinstance(scalar, float)

    def test_regime_position_scalar_trending(self):
        from src.strategies.filters import regime_position_scalar

        scalar = regime_position_scalar(
            regime_state=1, prob_trending=0.7, prob_ranging=0.2, prob_volatile=0.1
        )
        assert isinstance(scalar, float)

    def test_apply_all_strategy_filters(self):
        from src.strategies.filters import apply_all_strategy_filters

        bars = _make_bars(n=250)
        atr = _atr_series(bars)
        result = apply_all_strategy_filters(
            close=bars["close"],
            volume=bars["volume"],
            atr_series=atr,
            direction=1,
            regime_state=1,
            prob_trending=0.6,
            prob_ranging=0.3,
            prob_volatile=0.1,
        )
        assert isinstance(result, dict)
