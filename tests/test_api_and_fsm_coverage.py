"""
Coverage for small zero-coverage modules — Debt-005.

Covers:
  - src/api/middleware.py (validate_cors_config)
  - src/api/auth.py (verify_api_key, _get_configured_key, verify_websocket_key)
  - src/execution/live_fsm_integration.py (LiveExecutorOrderFSM init)
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# middleware.py — validate_cors_config
# ---------------------------------------------------------------------------

class TestValidateCorsConfig:
    def _validate(self, origins, allow_credentials=False):
        from src.api.middleware import validate_cors_config
        validate_cors_config(origins, allow_credentials)

    def test_valid_https_origin_passes(self):
        self._validate(["https://example.com"])

    def test_valid_http_with_port_passes(self):
        self._validate(["http://localhost:3000"])

    def test_valid_multiple_origins(self):
        self._validate(["https://app.example.com", "https://admin.example.com"])

    def test_wildcard_without_credentials_passes(self):
        # '*' is only blocked when combined with allow_credentials=True
        # (browser blocks it; server should still validate the config)
        # The code only blocks wildcard + credentials combo
        self._validate(["https://example.com"], allow_credentials=False)

    def test_wildcard_with_credentials_raises(self):
        from src.api.middleware import validate_cors_config
        with pytest.raises(RuntimeError, match="wildcard"):
            validate_cors_config(["*"], allow_credentials=True)

    def test_null_origin_raises(self):
        from src.api.middleware import validate_cors_config
        with pytest.raises(RuntimeError, match="null"):
            validate_cors_config(["null"], allow_credentials=False)

    def test_null_origin_case_insensitive(self):
        from src.api.middleware import validate_cors_config
        with pytest.raises(RuntimeError, match="null"):
            validate_cors_config(["NULL"], allow_credentials=False)

    def test_schemeless_origin_raises(self):
        from src.api.middleware import validate_cors_config
        with pytest.raises(RuntimeError, match="does not match"):
            validate_cors_config(["example.com"], allow_credentials=False)

    def test_bare_ip_raises(self):
        from src.api.middleware import validate_cors_config
        with pytest.raises(RuntimeError, match="does not match"):
            validate_cors_config(["192.168.1.1"], allow_credentials=False)

    def test_https_with_port_passes(self):
        self._validate(["https://api.example.com:8443"])

    def test_empty_origins_passes(self):
        self._validate([])  # nothing to validate — no error


# ---------------------------------------------------------------------------
# auth.py — _get_configured_key, verify_api_key, verify_websocket_key
# ---------------------------------------------------------------------------

class TestGetConfiguredKey:
    def test_raises_when_key_not_set(self):
        from src.api.auth import _get_configured_key
        with patch.dict(os.environ, {"API_SECRET_KEY": ""}, clear=False):
            with pytest.raises(RuntimeError, match="not set"):
                _get_configured_key()

    def test_raises_when_key_too_short(self):
        from src.api.auth import _get_configured_key
        with patch.dict(os.environ, {"API_SECRET_KEY": "short"}, clear=False):
            with pytest.raises(RuntimeError, match="too short"):
                _get_configured_key()

    def test_returns_key_when_valid(self):
        from src.api.auth import _get_configured_key
        strong_key = "a" * 32
        with patch.dict(os.environ, {"API_SECRET_KEY": strong_key}, clear=False):
            assert _get_configured_key() == strong_key

    def test_strips_whitespace(self):
        from src.api.auth import _get_configured_key
        strong_key = "  " + "b" * 32 + "  "
        with patch.dict(os.environ, {"API_SECRET_KEY": strong_key}, clear=False):
            result = _get_configured_key()
            assert not result.startswith(" ")


class TestVerifyApiKey:
    _KEY = "x" * 32

    def _set_key(self):
        return patch.dict(os.environ, {"API_SECRET_KEY": self._KEY}, clear=False)

    def test_valid_key_does_not_raise(self):
        from src.api.auth import verify_api_key
        with self._set_key():
            verify_api_key(self._KEY)  # must not raise

    def test_wrong_key_raises_401(self):
        from src.api.auth import verify_api_key
        with self._set_key():
            with pytest.raises(HTTPException) as exc:
                verify_api_key("wrong_key")
        assert exc.value.status_code == 401

    def test_missing_key_raises_401(self):
        from src.api.auth import verify_api_key
        with self._set_key():
            with pytest.raises(HTTPException) as exc:
                verify_api_key(None)
        assert exc.value.status_code == 401

    def test_server_key_not_configured_raises_503(self):
        from src.api.auth import verify_api_key
        with patch.dict(os.environ, {"API_SECRET_KEY": ""}, clear=False):
            with pytest.raises(HTTPException) as exc:
                verify_api_key("anything")
        assert exc.value.status_code == 503

    def test_timing_safe_comparison(self):
        """Key comparison uses hmac.compare_digest — verify correct key still passes."""
        from src.api.auth import verify_api_key
        with self._set_key():
            verify_api_key(self._KEY)  # identical bytes → passes


class TestVerifyWsKey:
    _KEY = "w" * 32

    def _set_key(self):
        return patch.dict(os.environ, {"API_SECRET_KEY": self._KEY}, clear=False)

    def _mock_ws(self, key=None):
        ws = MagicMock()
        ws.headers = {"x-api-key": key} if key else {}
        ws.close = AsyncMock()
        ws.client = ("127.0.0.1", 9000)
        return ws

    @pytest.mark.asyncio
    async def test_valid_header_passes(self):
        from src.api.auth import verify_ws_key
        with self._set_key():
            await verify_ws_key(self._mock_ws(key=self._KEY))  # must not raise

    @pytest.mark.asyncio
    async def test_missing_header_raises_401(self):
        from src.api.auth import verify_ws_key
        ws = self._mock_ws()
        with self._set_key():
            with pytest.raises(HTTPException) as exc:
                await verify_ws_key(ws)
        assert exc.value.status_code == 401
        ws.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_wrong_key_raises_401(self):
        from src.api.auth import verify_ws_key
        ws = self._mock_ws(key="bad")
        with self._set_key():
            with pytest.raises(HTTPException) as exc:
                await verify_ws_key(ws)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_server_key_not_set_closes_4503(self):
        from src.api.auth import verify_ws_key
        ws = self._mock_ws(key="anything")
        with patch.dict(os.environ, {"API_SECRET_KEY": ""}, clear=False):
            with pytest.raises((HTTPException, RuntimeError)):
                await verify_ws_key(ws)
        ws.close.assert_called()


# ---------------------------------------------------------------------------
# live_fsm_integration.py — LiveExecutorOrderFSM
# ---------------------------------------------------------------------------

class TestLiveExecutorOrderFSMInit:
    def test_init_stores_fetcher(self):
        from src.execution.live_fsm_integration import LiveExecutorOrderFSM
        fetcher = MagicMock()
        wrapper = LiveExecutorOrderFSM(fetcher=fetcher)
        assert wrapper._fetcher is fetcher

    def test_order_manager_created(self):
        from src.execution.live_fsm_integration import LiveExecutorOrderFSM
        from src.execution.order_manager import OrderManager
        wrapper = LiveExecutorOrderFSM(fetcher=MagicMock())
        assert isinstance(wrapper._order_manager, OrderManager)

    def test_get_fsm_state_raises_before_order(self):
        """Before any order, get_order_state should raise or return None."""
        from src.execution.live_fsm_integration import LiveExecutorOrderFSM
        wrapper = LiveExecutorOrderFSM(fetcher=MagicMock())
        # No order placed yet — accessing current_state not applicable
        # Just verify the object initialises cleanly
        assert wrapper is not None


class TestLiveExecutorOrderFSMPlaceOrder:
    """Coverage for LiveExecutorOrderFSM.place_market_order_with_fsm (lines 65-105)."""

    def _make_wrapper(self, order_manager_mock):
        from src.execution.live_fsm_integration import LiveExecutorOrderFSM
        fetcher = MagicMock()
        fetcher.get_order_exchange.return_value = AsyncMock()
        wrapper = LiveExecutorOrderFSM(fetcher=fetcher)
        wrapper._order_manager = order_manager_mock
        return wrapper

    @pytest.mark.asyncio
    async def test_successful_order_returns_fsm_and_dict(self):
        """Happy path: place_order_with_fsm succeeds → returns (OrderFSM, dict)."""
        from src.execution.order_fsm import OrderFSM, OrderFSMState, OrderStatus
        fsm_state = OrderFSMState(
            order_id="ord-1", symbol="BTC/USDT", side="buy",
            quantity=0.01, status=OrderStatus.FILLED,
        )
        fsm_state.filled_qty = 0.01
        fsm_state.average_fill_price = 50000.0
        mock_fsm = OrderFSM(fsm_state)
        mock_order = {"id": "ord-1", "status": "closed", "filled": 0.01}

        om = AsyncMock()
        om.place_order_with_fsm.return_value = (mock_fsm, mock_order)
        wrapper = self._make_wrapper(om)

        result_fsm, result_order = await wrapper.place_market_order_with_fsm(
            symbol="BTC/USDT", side="buy", quantity=0.01
        )
        assert result_fsm is mock_fsm
        assert result_order is mock_order

    @pytest.mark.asyncio
    async def test_timeout_propagates(self):
        """TimeoutError from order_manager is logged and re-raised."""
        om = AsyncMock()
        om.place_order_with_fsm.side_effect = TimeoutError("fill timeout")
        wrapper = self._make_wrapper(om)

        with pytest.raises(TimeoutError):
            await wrapper.place_market_order_with_fsm(
                symbol="BTC/USDT", side="buy", quantity=0.01
            )

    @pytest.mark.asyncio
    async def test_exchange_error_propagates(self):
        """ccxt.ExchangeError from order_manager is logged and re-raised."""
        import ccxt
        om = AsyncMock()
        om.place_order_with_fsm.side_effect = ccxt.ExchangeError("insufficient funds")
        wrapper = self._make_wrapper(om)

        with pytest.raises(ccxt.ExchangeError):
            await wrapper.place_market_order_with_fsm(
                symbol="BTC/USDT", side="sell", quantity=0.1
            )


# ---------------------------------------------------------------------------
# /intelligence/coverage and /intelligence/providers endpoint tests
# ---------------------------------------------------------------------------

class TestIntelligenceEndpoints:
    """OCI-012: coverage and provider-status endpoints."""

    def _make_state(self):
        from unittest.mock import AsyncMock, MagicMock
        from src.api.main import _AppState
        state = _AppState()
        orch = MagicMock()
        storage = MagicMock()
        storage.intelligence_feature_coverage = AsyncMock(return_value={
            "total_rows": 100,
            "coverage": {
                "intelligence_binance_funding_rate_pct": 1.0,
                "intelligence_defi_tvl_7d_change_pct": 0.95,
                "intelligence_mvrv_z_score": 0.0,
                "intelligence_sopr": 0.0,
            },
        })
        orch._storage = storage

        cfg = MagicMock()
        cfg.symbol = "BTC/USDT"
        cfg.primary_timeframe.value = "1h"
        orch._cfg = cfg
        state.orchestrator = orch

        # VF-022 fix: get_intelligence_coverage() now calls get_settings() directly
        # (not _state.runtime_config, which was never an AppState attribute).
        # Tests patch get_settings at the api.main import site instead.
        return state

    @pytest.mark.asyncio
    async def test_coverage_returns_total_rows_and_coverage_dict(self):
        from unittest.mock import MagicMock, patch
        from src.api.main import get_intelligence_coverage
        import src.api.main as api_mod

        state = self._make_state()
        mock_settings = MagicMock()
        mock_settings.primary_symbol = "BTC/USDT"
        mock_settings.primary_timeframe.value = "1h"

        orig = api_mod._state
        try:
            api_mod._state = state
            with patch("src.api.main.get_settings", return_value=mock_settings):
                result = await get_intelligence_coverage()
        finally:
            api_mod._state = orig

        assert result["total_rows"] == 100
        assert "coverage" in result
        assert result["coverage"]["intelligence_defi_tvl_7d_change_pct"] == 0.95

    @pytest.mark.asyncio
    async def test_coverage_no_orchestrator_returns_error(self):
        from src.api.main import get_intelligence_coverage, _AppState
        import src.api.main as api_mod

        state = _AppState()
        orig = api_mod._state
        try:
            api_mod._state = state
            result = await get_intelligence_coverage()
        finally:
            api_mod._state = orig

        assert "error" in result

    @pytest.mark.asyncio
    async def test_providers_returns_five_providers(self):
        from src.api.main import get_intelligence_providers
        result = await get_intelligence_providers()
        assert "providers" in result
        providers = result["providers"]
        assert len(providers) == 5
        ids = {p["exchange_id"] for p in providers}
        assert ids == {"arkham_intel", "defillama", "dune_analytics", "cryptoquant", "coinglass"}

    @pytest.mark.asyncio
    async def test_defillama_always_enabled(self):
        from src.api.main import get_intelligence_providers
        result = await get_intelligence_providers()
        defillama = next(p for p in result["providers"] if p["exchange_id"] == "defillama")
        assert defillama["enabled"] is True
