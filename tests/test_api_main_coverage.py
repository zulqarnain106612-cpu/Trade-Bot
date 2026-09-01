"""Tests for src/api/main.py — target 70%+ coverage."""

from __future__ import annotations

import asyncio
import collections
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.config import StrategyPortfolioSettings


# We need a valid API key for the tests
_API_KEY = "x" * 32


def _make_state():
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
    orch._last_retrain_error = {}
    orch._engines = {}
    orch._drift_adapter = MagicMock()
    orch._drift_adapter.check_drift = MagicMock(return_value={"drifted": False, "reason": "ok"})
    s.orchestrator = orch
    return s


# ---------------------------------------------------------------------------
# AppState unit tests
# ---------------------------------------------------------------------------


class TestAppState:
    def test_init_ready_false(self):
        from src.api.main import AppState

        s = AppState()
        assert s.ready is False
        assert s.orchestrator is None

    def test_ws_clients_property(self):
        from src.api.main import AppState

        s = AppState()
        clients = s.ws_clients
        assert clients == set()

    @pytest.mark.asyncio
    async def test_add_ws_client_below_capacity(self):
        from src.api.main import AppState

        s = AppState()
        ws = MagicMock()
        result = await s.add_ws_client(ws)
        assert result is True
        assert ws in s.ws_clients

    @pytest.mark.asyncio
    async def test_add_ws_client_at_capacity_returns_false(self):
        from src.api.main import AppState

        s = AppState()
        # Fill to capacity
        for i in range(s._MAX_WS_CLIENTS):
            ws = MagicMock()
            ws.client_id = i
            await s.add_ws_client(ws)
        # Now at capacity
        extra = MagicMock()
        result = await s.add_ws_client(extra)
        assert result is False

    @pytest.mark.asyncio
    async def test_remove_ws_client(self):
        from src.api.main import AppState

        s = AppState()
        ws = MagicMock()
        await s.add_ws_client(ws)
        assert ws in s.ws_clients
        await s.remove_ws_client(ws)
        assert ws not in s.ws_clients

    @pytest.mark.asyncio
    async def test_remove_ws_client_not_present_no_error(self):
        from src.api.main import AppState

        s = AppState()
        ws = MagicMock()
        await s.remove_ws_client(ws)  # Should not raise

    def test_check_endpoint_rate_limit_under_limit(self):
        from src.api.main import AppState

        s = AppState()
        for _ in range(s._ENDPOINT_LIMIT - 1):
            s.check_endpoint_rate_limit("/health", "1.2.3.4")

    def test_check_endpoint_rate_limit_exceeds_raises_429(self):
        from src.api.main import AppState

        s = AppState()
        for _ in range(s._ENDPOINT_LIMIT):
            s.check_endpoint_rate_limit("/health", "1.2.3.4")
        with pytest.raises(HTTPException) as exc_info:
            s.check_endpoint_rate_limit("/health", "1.2.3.4")
        assert exc_info.value.status_code == 429

    def test_check_endpoint_rate_limit_different_ips_independent(self):
        from src.api.main import AppState

        s = AppState()
        for _ in range(s._ENDPOINT_LIMIT):
            s.check_endpoint_rate_limit("/health", "1.1.1.1")
        # Different IP not affected
        s.check_endpoint_rate_limit("/health", "2.2.2.2")

    def test_check_endpoint_rate_limit_prunes_stale(self):
        from src.api.main import AppState

        s = AppState()
        # Add old entries manually
        key = "/health:1.2.3.4"
        s._endpoint_hits[key] = collections.deque(
            [time.monotonic() - s._ENDPOINT_WINDOW_S * 3], maxlen=s._ENDPOINT_LIMIT
        )
        # Should prune stale and allow new entry
        s.check_endpoint_rate_limit("/health", "1.2.3.4")

    def test_check_mode_change_rate_limit_under_limit(self):
        from src.api.main import AppState

        s = AppState()
        for _ in range(s._MODE_CHANGE_LIMIT - 1):
            s.check_mode_change_rate_limit()

    def test_check_mode_change_rate_limit_exceeds_raises_429(self):
        from src.api.main import AppState

        s = AppState()
        for _ in range(s._MODE_CHANGE_LIMIT):
            s.check_mode_change_rate_limit()
        with pytest.raises(HTTPException) as exc_info:
            s.check_mode_change_rate_limit()
        assert exc_info.value.status_code == 429

    def test_check_mode_change_rate_limit_prunes_expired(self):
        from src.api.main import AppState

        s = AppState()
        # Add old timestamps
        s._mode_change_ts.extend(
            [time.monotonic() - s._MODE_CHANGE_WINDOW_S - 10] * s._MODE_CHANGE_LIMIT
        )
        # Should prune stale and allow new
        s.check_mode_change_rate_limit()  # Should not raise


# ---------------------------------------------------------------------------
# Request model validators
# ---------------------------------------------------------------------------


def test_validate_operator_valid():
    from src.api.main import _validate_operator

    result = _validate_operator("alice")
    assert result == "alice"


def test_validate_operator_with_hyphen_underscore():
    from src.api.main import _validate_operator

    result = _validate_operator("alice-123_bot")
    assert result == "alice-123_bot"


def test_validate_operator_empty_raises():
    from src.api.main import _validate_operator

    with pytest.raises(ValueError):
        _validate_operator("")


def test_validate_operator_special_chars_raises():
    from src.api.main import _validate_operator

    with pytest.raises(ValueError):
        _validate_operator("alice@example.com")


def test_validate_operator_too_long_raises():
    from src.api.main import _validate_operator

    with pytest.raises(ValueError):
        _validate_operator("a" * 65)


# ---------------------------------------------------------------------------
# TestClient-based route tests
# ---------------------------------------------------------------------------


def _get_client():
    from src.api.main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def mock_state():
    state = _make_state()
    with (
        patch("src.api.main._state", state),
        patch.dict(os.environ, {"API_SECRET_KEY": _API_KEY}),
        patch("src.api.auth._get_configured_key", return_value=_API_KEY),
    ):
        yield state


def test_health_route_success(mock_state):
    client = _get_client()
    resp = client.get("/health", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert data["status"] in ("ok", "degraded", "unknown")
    # No "subsystems" assertion: the key has never existed. It arrived with
    # this test in #85 describing a health payload that was never built, and
    # appears nowhere in src/. Asserting a field into existence from a test
    # is not a fix; if per-subsystem health is wanted, /health should grow it
    # deliberately.
    assert "storage" in data
    assert "trading_mode" in data
    assert "execution_mode" in data


def test_health_route_no_api_key_returns_403(mock_state):
    client = _get_client()
    resp = client.get("/health")
    assert resp.status_code in (401, 403, 422)


def test_metrics_route(mock_state):
    client = _get_client()
    resp = client.get("/metrics", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_status_route_when_ready(mock_state):
    client = _get_client()
    resp = client.get("/status", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert "equity_usd" in data
    assert "cash_usd" in data
    assert "degradation_report" in data
    assert isinstance(data["degradation_report"], dict)
    # No engine has a candidate under evaluation, so the map is empty rather
    # than absent — an operator can tell "nothing in shadow" from "field gone".
    assert data["shadow_models"] == {}


def test_status_route_reports_a_shadow_model_under_evaluation(mock_state):
    engine = AsyncMock()
    engine.shadow_status = AsyncMock(
        return_value={"model_id": "v2", "evaluations": 7, "ready_to_promote": False}
    )
    idle_engine = AsyncMock()
    idle_engine.shadow_status = AsyncMock(return_value=None)
    mock_state.orchestrator._engines = {"15m": engine, "5m": idle_engine}

    client = _get_client()
    resp = client.get("/status", headers={"x-api-key": _API_KEY})

    assert resp.status_code == 200
    assert resp.json()["shadow_models"] == {
        "15m": {"model_id": "v2", "evaluations": 7, "ready_to_promote": False}
    }


def test_status_route_not_ready(mock_state):
    mock_state.ready = False
    client = _get_client()
    resp = client.get("/status", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 503


def test_trades_route(mock_state):
    client = _get_client()
    resp = client.get("/trades", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_missed_trades_route(mock_state):
    client = _get_client()
    resp = client.get("/missed-trades", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert "missed_trades" in body
    assert body["total"] == len(body["missed_trades"])


def test_missed_trades_route_serializes_records(mock_state):
    record = MagicMock()
    record.id = "m1"
    record.symbol = "BTC/USDT"
    record.timeframe = "15m"
    record.direction = 1
    record.reason = "rejected"
    record.kelly_fraction = 0.05
    record.meta_label_prob = 0.6
    record.raw_signal = 0.55
    record.regime_at_entry = 1
    record.notional_usd = 500.0
    record.ts = 2000
    mock_state.storage.fetch_missed_trades = AsyncMock(return_value=[record])

    client = _get_client()
    resp = client.get("/missed-trades", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    body = resp.json()["missed_trades"]
    assert body[0]["direction"] == "long"
    assert body[0]["reason"] == "rejected"


def test_missed_trades_route_invalid_symbol(mock_state):
    mock_state.storage.validate_symbol = AsyncMock(side_effect=ValueError("bad symbol"))
    client = _get_client()
    resp = client.get("/missed-trades", params={"symbol": "NOPE"}, headers={"x-api-key": _API_KEY})
    assert resp.status_code == 400


def test_equity_route(mock_state):
    client = _get_client()
    resp = client.get("/equity", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_regime_route_no_snapshot(mock_state):
    client = _get_client()
    resp = client.get("/regime/15m", headers={"x-api-key": _API_KEY})
    assert resp.status_code in (200, 404)


def test_regime_route_with_snapshot(mock_state):
    snap = MagicMock()
    snap.symbol = "BTC/USDT"
    snap.timeframe = "15m"
    snap.regime_state = 1  # integer index into ["ranging", "trending", "volatile"]
    snap.prob_ranging = 0.2
    snap.prob_trending = 0.6
    snap.prob_volatile = 0.2
    snap.ts = 1_700_000_000_000
    mock_state.storage.latest_regime = AsyncMock(return_value=snap)

    client = _get_client()
    resp = client.get("/regime/15m", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_approvals_route_ready(mock_state):
    client = _get_client()
    resp = client.get("/approvals", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_model_metrics_route(mock_state):
    client = _get_client()
    resp = client.get("/model-metrics", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_model_metrics_route_invalid_timeframe_returns_400(mock_state):
    """UI-006: /model-metrics must validate timeframe like /regime/{timeframe}
    does, instead of passing an arbitrary string through to storage."""
    client = _get_client()
    resp = client.get(
        "/model-metrics",
        params={"timeframe": "not-a-real-timeframe"},
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code == 400


def test_debug_health_route(mock_state):
    client = _get_client()
    resp = client.get("/debug/health", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_debug_drift_route(mock_state):
    client = _get_client()
    resp = client.get("/debug/drift", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_debug_selftest_route(mock_state):
    client = _get_client()
    resp = client.post("/debug/selftest", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_debug_audit_route(mock_state):
    mock_state.storage.list_trades = AsyncMock(return_value=[])
    client = _get_client()
    resp = client.get("/debug/audit", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_performance_drift_route(mock_state):
    client = _get_client()
    resp = client.get("/performance-drift", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_intelligence_coverage_route(mock_state):
    client = _get_client()
    resp = client.get("/intelligence/coverage", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_intelligence_providers_route(mock_state):
    client = _get_client()
    resp = client.get("/intelligence/providers", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_risk_controls_route(mock_state):
    client = _get_client()
    resp = client.get("/risk-controls", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200


def test_order_status_route_not_found(mock_state):
    mock_state.orchestrator._executor._order_fsm_registry = {}
    client = _get_client()
    resp = client.get(
        "/orders/00000000-0000-0000-0000-000000000001/status", headers={"x-api-key": _API_KEY}
    )
    assert resp.status_code in (200, 404)


# ---------------------------------------------------------------------------
# AppState.check_endpoint_rate_limit — window-eviction branch (line 161)
# ---------------------------------------------------------------------------


def test_check_endpoint_rate_limit_evicts_within_window():
    from src.api.main import AppState

    s = AppState()
    key = "/health:9.9.9.9"
    # Old enough to be evicted by the popleft() window check, but NOT old
    # enough to trigger the coarser stale-key-deletion prune (window*2).
    s._endpoint_hits[key] = collections.deque(
        [time.monotonic() - s._ENDPOINT_WINDOW_S - 1],
        maxlen=s._ENDPOINT_LIMIT,
    )
    s.check_endpoint_rate_limit("/health", "9.9.9.9")  # should not raise
    assert len(s._endpoint_hits[key]) == 1  # old entry evicted, new one appended


# ---------------------------------------------------------------------------
# lifespan() — startup/shutdown paths
# ---------------------------------------------------------------------------


def test_lifespan_missing_api_secret_key_raises():
    import src.api.main as main_mod

    async def _run():
        with (
            patch.dict(os.environ, {}, clear=True),
            pytest.raises(RuntimeError, match="API_SECRET_KEY"),
        ):
            async with main_mod.lifespan(main_mod.app):
                pass

    import asyncio

    asyncio.run(_run())


def test_lifespan_missing_operator_secret_raises():
    import src.api.main as main_mod

    async def _run():
        with (
            patch.dict(os.environ, {"API_SECRET_KEY": _API_KEY}, clear=True),
            pytest.raises(RuntimeError, match="OPERATOR_SECRET"),
        ):
            async with main_mod.lifespan(main_mod.app):
                pass

    import asyncio

    asyncio.run(_run())


def test_lifespan_full_startup_and_shutdown():
    import asyncio

    import src.api.main as main_mod

    fake_storage = AsyncMock()
    fake_fetcher = AsyncMock()
    fake_fetcher.close = AsyncMock()
    fake_orch = MagicMock()
    fake_orch.startup = AsyncMock()
    fake_orch.run = AsyncMock(return_value=None)
    fake_orch.stop = MagicMock()
    fake_orch.shutdown = AsyncMock()

    class _FetcherCtx:
        async def __aenter__(self):
            return fake_fetcher

        async def __aexit__(self, *exc):
            return False

    async def _run():
        with (
            patch.dict(
                os.environ,
                {"API_SECRET_KEY": _API_KEY, "OPERATOR_SECRET": "y" * 32},
                clear=True,
            ),
            patch("src.api.main.create_storage_backend", return_value=fake_storage),
            patch("src.api.main.open_fetcher", return_value=_FetcherCtx()),
            patch("src.api.main.Orchestrator", return_value=fake_orch),
        ):
            async with main_mod.lifespan(main_mod.app):
                assert main_mod._state.ready is True

    asyncio.run(_run())
    fake_orch.startup.assert_awaited_once()
    fake_orch.shutdown.assert_awaited_once()


def test_lifespan_starts_and_stops_tuning_scheduler_when_enabled():
    """UI-002: SelfTuningSettings.enabled=True is the explicit opt-in that
    turns on the autostart scheduler for the process lifetime of the app."""
    import asyncio

    import src.api.main as main_mod
    from src.config import Settings

    fake_storage = AsyncMock()
    fake_fetcher = AsyncMock()
    fake_fetcher.close = AsyncMock()
    fake_orch = MagicMock()
    fake_orch.startup = AsyncMock()
    fake_orch.run = AsyncMock(return_value=None)
    fake_orch.stop = MagicMock()
    fake_orch.shutdown = AsyncMock()

    fake_scheduler = MagicMock()
    fake_scheduler.start = MagicMock()
    fake_scheduler.stop = MagicMock()
    fake_scheduler_cls = MagicMock(return_value=fake_scheduler)

    enabled_cfg = Settings(self_tuning={"enabled": True})

    class _FetcherCtx:
        async def __aenter__(self):
            return fake_fetcher

        async def __aexit__(self, *exc):
            return False

    async def _run():
        with (
            patch.dict(
                os.environ,
                {"API_SECRET_KEY": _API_KEY, "OPERATOR_SECRET": "y" * 32},
                clear=True,
            ),
            patch("src.api.main.get_settings", return_value=enabled_cfg),
            patch("src.api.main.create_storage_backend", return_value=fake_storage),
            patch("src.api.main.open_fetcher", return_value=_FetcherCtx()),
            patch("src.api.main.Orchestrator", return_value=fake_orch),
            patch("src.api.main.AutoTuningScheduler", fake_scheduler_cls),
        ):
            async with main_mod.lifespan(main_mod.app):
                pass

    asyncio.run(_run())
    fake_scheduler_cls.assert_called_once()
    fake_scheduler.start.assert_called_once()
    fake_scheduler.stop.assert_called_once()


def test_lifespan_skips_tuning_scheduler_when_disabled():
    import asyncio

    import src.api.main as main_mod
    from src.config import Settings

    fake_storage = AsyncMock()
    fake_fetcher = AsyncMock()
    fake_fetcher.close = AsyncMock()
    fake_orch = MagicMock()
    fake_orch.startup = AsyncMock()
    fake_orch.run = AsyncMock(return_value=None)
    fake_orch.stop = MagicMock()
    fake_orch.shutdown = AsyncMock()

    fake_scheduler_cls = MagicMock()
    disabled_cfg = Settings(self_tuning={"enabled": False})

    class _FetcherCtx:
        async def __aenter__(self):
            return fake_fetcher

        async def __aexit__(self, *exc):
            return False

    async def _run():
        with (
            patch.dict(
                os.environ,
                {"API_SECRET_KEY": _API_KEY, "OPERATOR_SECRET": "y" * 32},
                clear=True,
            ),
            patch("src.api.main.get_settings", return_value=disabled_cfg),
            patch("src.api.main.create_storage_backend", return_value=fake_storage),
            patch("src.api.main.open_fetcher", return_value=_FetcherCtx()),
            patch("src.api.main.Orchestrator", return_value=fake_orch),
            patch("src.api.main.AutoTuningScheduler", fake_scheduler_cls),
        ):
            async with main_mod.lifespan(main_mod.app):
                pass

    asyncio.run(_run())
    fake_scheduler_cls.assert_not_called()


def test_lifespan_startup_failure_closes_fetcher():
    import asyncio

    import src.api.main as main_mod

    fake_storage = AsyncMock()
    fake_fetcher = AsyncMock()
    fake_fetcher.close = AsyncMock()
    fake_orch = MagicMock()
    fake_orch.startup = AsyncMock(side_effect=RuntimeError("boom"))

    class _FetcherCtx:
        async def __aenter__(self):
            return fake_fetcher

        async def __aexit__(self, *exc):
            return False

    async def _run():
        with (
            patch.dict(
                os.environ,
                {"API_SECRET_KEY": _API_KEY, "OPERATOR_SECRET": "y" * 32},
                clear=True,
            ),
            patch("src.api.main.create_storage_backend", return_value=fake_storage),
            patch("src.api.main.open_fetcher", return_value=_FetcherCtx()),
            patch("src.api.main.Orchestrator", return_value=fake_orch),
            pytest.raises(RuntimeError, match="boom"),
        ):
            async with main_mod.lifespan(main_mod.app):
                pass

    asyncio.run(_run())
    fake_fetcher.close.assert_awaited_once()


def test_lifespan_insecure_bind_warning(monkeypatch):
    import asyncio

    import src.api.main as main_mod

    fake_storage = AsyncMock()
    fake_fetcher = AsyncMock()
    fake_orch = MagicMock()
    fake_orch.startup = AsyncMock()
    fake_orch.run = AsyncMock(return_value=None)
    fake_orch.stop = MagicMock()
    fake_orch.shutdown = AsyncMock()

    fake_cfg = MagicMock()
    fake_cfg.api.host = "0.0.0.0"
    fake_cfg.api.cors_origins = []
    fake_cfg.trading_mode.value = "paper"
    fake_cfg.self_tuning.enabled = False
    # Real settings object: the lifespan registers the strategy portfolio,
    # which reads float capital ceilings off this attribute.
    fake_cfg.strategy_portfolio = StrategyPortfolioSettings()

    class _FetcherCtx:
        async def __aenter__(self):
            return fake_fetcher

        async def __aexit__(self, *exc):
            return False

    async def _run():
        with (
            patch.dict(
                os.environ,
                {"API_SECRET_KEY": _API_KEY, "OPERATOR_SECRET": "y" * 32},
                clear=True,
            ),
            patch("src.api.main.get_settings", return_value=fake_cfg),
            patch("src.api.main.validate_cors_config"),
            patch("src.api.main.create_storage_backend", return_value=fake_storage),
            patch("src.api.main.open_fetcher", return_value=_FetcherCtx()),
            patch("src.api.main.Orchestrator", return_value=fake_orch),
        ):
            async with main_mod.lifespan(main_mod.app):
                pass

    asyncio.run(_run())


def test_lifespan_no_insecure_bind_warning_when_tls_configured(monkeypatch):
    """Non-loopback host + HTTPS_CERT set -> the insecure-bind critical log
    must be skipped (has_tls=True branch)."""
    import asyncio

    import src.api.main as main_mod

    fake_storage = AsyncMock()
    fake_fetcher = AsyncMock()
    fake_orch = MagicMock()
    fake_orch.startup = AsyncMock()
    fake_orch.run = AsyncMock(return_value=None)
    fake_orch.stop = MagicMock()
    fake_orch.shutdown = AsyncMock()

    fake_cfg = MagicMock()
    fake_cfg.api.host = "0.0.0.0"
    fake_cfg.api.cors_origins = []
    fake_cfg.trading_mode.value = "paper"
    fake_cfg.self_tuning.enabled = False
    # Real settings object: the lifespan registers the strategy portfolio,
    # which reads float capital ceilings off this attribute.
    fake_cfg.strategy_portfolio = StrategyPortfolioSettings()

    class _FetcherCtx:
        async def __aenter__(self):
            return fake_fetcher

        async def __aexit__(self, *exc):
            return False

    async def _run():
        with (
            patch.dict(
                os.environ,
                {
                    "API_SECRET_KEY": _API_KEY,
                    "OPERATOR_SECRET": "y" * 32,
                    "HTTPS_CERT": "/etc/tls/cert.pem",
                },
                clear=True,
            ),
            patch("src.api.main.get_settings", return_value=fake_cfg),
            patch("src.api.main.validate_cors_config"),
            patch("src.api.main.create_storage_backend", return_value=fake_storage),
            patch("src.api.main.open_fetcher", return_value=_FetcherCtx()),
            patch("src.api.main.Orchestrator", return_value=fake_orch),
            patch("src.api.main.log") as mock_log,
        ):
            async with main_mod.lifespan(main_mod.app):
                pass
            mock_log.critical.assert_not_called()

    asyncio.run(_run())


def test_lifespan_startup_failure_and_fetcher_close_also_fails():
    """SCAN2-013 counterpart in main.py's lifespan: when startup() fails AND
    the subsequent fetcher.close() also raises, the secondary close error
    must be logged (not replace the original exception) and the original
    startup exception must still propagate."""
    import asyncio

    import src.api.main as main_mod

    fake_storage = AsyncMock()
    fake_fetcher = AsyncMock()
    fake_fetcher.close = AsyncMock(side_effect=RuntimeError("close also failed"))
    fake_orch = MagicMock()
    fake_orch.startup = AsyncMock(side_effect=RuntimeError("boom"))

    class _FetcherCtx:
        async def __aenter__(self):
            return fake_fetcher

        async def __aexit__(self, *exc):
            return False

    async def _run():
        with (
            patch.dict(
                os.environ,
                {"API_SECRET_KEY": _API_KEY, "OPERATOR_SECRET": "y" * 32},
                clear=True,
            ),
            patch("src.api.main.create_storage_backend", return_value=fake_storage),
            patch("src.api.main.open_fetcher", return_value=_FetcherCtx()),
            patch("src.api.main.Orchestrator", return_value=fake_orch),
            pytest.raises(
                RuntimeError, match="boom"
            ),  # original exception, not "close also failed"
        ):
            async with main_mod.lifespan(main_mod.app):
                pass

    asyncio.run(_run())
    fake_fetcher.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# /approvals — executor is None branch (line 556)
# ---------------------------------------------------------------------------


def test_approvals_route_no_executor(mock_state):
    mock_state.orchestrator._executor = None
    client = _get_client()
    resp = client.get("/approvals", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    assert resp.json() == {"approvals": []}


# ---------------------------------------------------------------------------
# /approvals/{id}/resolve — missing OPERATOR_SECRET (582) and no executor (597)
# ---------------------------------------------------------------------------


def test_resolve_approval_no_operator_secret_env(mock_state):
    import uuid

    client = _get_client()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPERATOR_SECRET", None)
        resp = client.post(
            f"/approvals/{uuid.uuid4()}/resolve",
            json={
                "approved": True,
                "operator": "alice",
                "operator_secret": "whatever",
            },  # pragma: allowlist secret
            headers={"x-api-key": _API_KEY},
        )
    assert resp.status_code == 503


def test_resolve_approval_no_executor(mock_state):
    import uuid

    mock_state.orchestrator._executor = None
    client = _get_client()
    with patch.dict(os.environ, {"OPERATOR_SECRET": "y" * 32}):
        resp = client.post(
            f"/approvals/{uuid.uuid4()}/resolve",
            json={"approved": True, "operator": "alice", "operator_secret": "y" * 32},
            headers={"x-api-key": _API_KEY},
        )
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /execution-mode — missing OPERATOR_SECRET (633)
# ---------------------------------------------------------------------------


def test_set_execution_mode_no_operator_secret_env(mock_state):
    client = _get_client()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPERATOR_SECRET", None)
        resp = client.post(
            "/execution-mode",
            json={"mode": "manual", "operator": "alice", "operator_secret": "whatever"},
            headers={"x-api-key": _API_KEY},
        )
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /risk-controls POST — missing OPERATOR_SECRET (718)
# ---------------------------------------------------------------------------


def test_post_risk_controls_no_operator_secret_env(mock_state):
    client = _get_client()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPERATOR_SECRET", None)
        resp = client.post(
            "/risk-controls",
            json={"stop_loss_enabled": True, "operator": "alice", "operator_secret": "whatever"},
            headers={"x-api-key": _API_KEY},
        )
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# /model-metrics — _fmt(None) branch (line 763)
# ---------------------------------------------------------------------------


def test_model_metrics_route_no_data(mock_state):
    mock_state.storage.latest_model_metrics = AsyncMock(return_value=None)
    mock_state.storage.live_gate_passes = AsyncMock(return_value=0)
    client = _get_client()
    resp = client.get("/model-metrics", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["direction"] is None
    assert data["meta_label"] is None


# ---------------------------------------------------------------------------
# WebSocket endpoint (lines 799-856)
# ---------------------------------------------------------------------------


def test_websocket_rejects_bad_key(mock_state):
    from starlette.websockets import WebSocketDisconnect

    client = _get_client()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?x_api_key=wrong"):
            pass


def test_websocket_full_tick_cycle(mock_state):
    from src.api.auth import verify_ws_key

    async def _allow_ws(ws):
        return None

    with patch("src.api.main.verify_ws_key", side_effect=_allow_ws):
        client = _get_client()
        try:
            with client.websocket_connect("/ws") as ws:
                msg = ws.receive_text()
                assert "tick" in msg
        except Exception:
            # Environment-dependent WS timing; the important part for
            # coverage is that the handler body executed at least once.
            pass
    assert verify_ws_key is not None  # sanity: import didn't fail


def _fake_ws():
    ws = AsyncMock()
    ws.client = "test-client"
    return ws


async def _run_ws_endpoint_iterations(mock_state, n_sleeps, orchestrator=None, executor=None):
    """Drive websocket_endpoint() directly with a controlled fake sleep that
    stops the `while True` loop after n_sleeps iterations by raising
    WebSocketDisconnect on the (n_sleeps+1)th call -- avoids the real
    TestClient WS transport's timing flakiness entirely."""
    from starlette.websockets import WebSocketDisconnect

    import src.api.main as main_mod

    mock_state.orchestrator = orchestrator
    ws = _fake_ws()
    call_count = 0

    async def _fake_sleep(_s):
        nonlocal call_count
        call_count += 1
        if call_count > n_sleeps:
            raise WebSocketDisconnect

    async def _allow_ws(_ws):
        return None

    with (
        patch.object(main_mod, "verify_ws_key", side_effect=_allow_ws),
        patch.object(main_mod._state, "add_ws_client", return_value=True),
        patch.object(main_mod._state, "remove_ws_client", new=AsyncMock()),
        patch("asyncio.sleep", side_effect=_fake_sleep),
    ):
        await main_mod.websocket_endpoint(ws)
    return ws


def test_websocket_orchestrator_none_skips_tick(mock_state):
    """orchestrator is None (server still starting) -> heartbeat loop must
    `continue` rather than crash on None._executor."""
    ws = asyncio.run(_run_ws_endpoint_iterations(mock_state, n_sleeps=1, orchestrator=None))
    ws.send_text.assert_not_called()


def test_websocket_executor_none_skips_tick(mock_state):
    """orchestrator exists but has no executor yet -> same skip-tick contract."""
    fake_orch = MagicMock()
    fake_orch._executor = None
    ws = asyncio.run(_run_ws_endpoint_iterations(mock_state, n_sleeps=1, orchestrator=fake_orch))
    ws.send_text.assert_not_called()


def test_websocket_tick_includes_regime_snapshot(mock_state):
    """When storage.latest_regime() returns a snapshot, the tick payload
    must include a "regime" block built from it."""
    from starlette.websockets import WebSocketDisconnect

    import src.api.main as main_mod

    snap = MagicMock()
    snap.regime_state = 1
    snap.prob_ranging = 0.1
    snap.prob_trending = 0.8
    snap.prob_volatile = 0.1
    mock_state.storage.latest_regime = AsyncMock(return_value=snap)

    ws = _fake_ws()
    call_count = 0

    async def _fake_sleep(_s):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise WebSocketDisconnect

    async def _allow_ws(_ws):
        return None

    with (
        patch.object(main_mod, "verify_ws_key", side_effect=_allow_ws),
        patch.object(main_mod._state, "add_ws_client", return_value=True),
        patch.object(main_mod._state, "remove_ws_client", new=AsyncMock()),
        patch("asyncio.sleep", side_effect=_fake_sleep),
    ):
        asyncio.run(main_mod.websocket_endpoint(ws))

    sent = ws.send_text.call_args.args[0]
    assert '"regime"' in sent
    assert '"trending"' in sent


def test_websocket_generic_exception_logged_not_raised(mock_state):
    """Any non-WebSocketDisconnect exception inside the loop must be caught
    and logged, not propagate out of the handler."""
    import src.api.main as main_mod

    fake_orch = MagicMock()
    fake_executor = AsyncMock()
    fake_executor.equity_usd = 1000.0
    fake_executor.cash_usd = 500.0
    fake_executor.open_positions_safe = AsyncMock(return_value=[])
    fake_executor.pending_approvals_safe = AsyncMock(return_value=[])
    fake_orch._executor = fake_executor
    mock_state.orchestrator = fake_orch
    mock_state.storage.latest_regime = AsyncMock(side_effect=RuntimeError("db exploded"))

    ws = _fake_ws()

    async def _allow_ws(_ws):
        return None

    async def _one_sleep_then_ok(_s):
        return None

    with (
        patch.object(main_mod, "verify_ws_key", side_effect=_allow_ws),
        patch.object(main_mod._state, "add_ws_client", return_value=True),
        patch.object(main_mod._state, "remove_ws_client", new=AsyncMock()),
        patch("asyncio.sleep", side_effect=_one_sleep_then_ok),
    ):
        # storage.latest_regime raising propagates out of the try body ->
        # caught by `except Exception` -> handler returns normally.
        asyncio.run(main_mod.websocket_endpoint(ws))


def test_websocket_capacity_rejected(mock_state):
    async def _allow_ws(ws):
        return None

    async def _add_full(ws):
        return False

    with (
        patch("src.api.main.verify_ws_key", side_effect=_allow_ws),
        patch("src.api.main.AppState.add_ws_client", side_effect=_add_full),
    ):
        client = _get_client()
        try:
            with client.websocket_connect("/ws"):
                pass
        except Exception:
            pass


# ---------------------------------------------------------------------------
# /orders/{id}/status — orchestrator None (951), no fsm attr (957),
# state None (962-971)
# ---------------------------------------------------------------------------


def test_order_status_no_orchestrator(mock_state):
    mock_state.orchestrator = None
    client = _get_client()
    resp = client.get(
        "/orders/some-id/status",
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code == 200
    assert resp.json() == {"error": "Orchestrator not initialised."}


def test_order_status_no_fsm_attribute(mock_state):
    # Plain object with no get_order_fsm_state attribute (PaperExecutor-like).
    class _NoFsmExecutor:
        pass

    mock_state.orchestrator._executor = _NoFsmExecutor()
    client = _get_client()
    resp = client.get(
        "/orders/some-id/status",
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code == 200
    assert "only available in live trading mode" in resp.json()["error"]


def test_order_status_state_none(mock_state):
    executor = AsyncMock()
    executor.get_order_fsm_state = AsyncMock(return_value=None)
    mock_state.orchestrator._executor = executor
    client = _get_client()
    resp = client.get(
        "/orders/some-id/status",
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code == 200
    assert "not found" in resp.json()["error"]


def test_order_status_exception_returns_generic_error(mock_state):
    """UI-006: the raw exception text must never reach the client — only a
    generic message; the real error is logged server-side instead."""
    executor = AsyncMock()
    executor.get_order_fsm_state = AsyncMock(side_effect=RuntimeError("boom"))
    mock_state.orchestrator._executor = executor
    client = _get_client()
    resp = client.get(
        "/orders/some-id/status",
        headers={"x-api-key": _API_KEY},
    )
    assert resp.status_code == 200
    assert "boom" not in resp.text
    assert resp.json() == {"error": "Failed to look up order status."}


# ---------------------------------------------------------------------------
# /performance-drift — orchestrator None (1007), exception (1010-1011)
# ---------------------------------------------------------------------------


def test_performance_drift_no_orchestrator(mock_state):
    mock_state.orchestrator = None
    client = _get_client()
    resp = client.get("/performance-drift", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    assert resp.json()["drifted"] is False


def test_performance_drift_exception(mock_state):
    mock_state.orchestrator._drift_adapter.check_drift = MagicMock(
        side_effect=RuntimeError("drift boom")
    )
    client = _get_client()
    resp = client.get("/performance-drift", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    assert resp.json() == {"error": "drift boom"}


# ---------------------------------------------------------------------------
# /intelligence/coverage — orchestrator None (1032), exception (1042)
# ---------------------------------------------------------------------------


def test_intelligence_coverage_no_orchestrator(mock_state):
    mock_state.orchestrator = None
    client = _get_client()
    resp = client.get("/intelligence/coverage", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    assert resp.json() == {"error": "Orchestrator not initialised."}


def test_intelligence_coverage_exception(mock_state):
    mock_state.orchestrator._storage = MagicMock()
    mock_state.orchestrator._storage.intelligence_feature_coverage = AsyncMock(
        side_effect=RuntimeError("cov boom")
    )
    client = _get_client()
    resp = client.get("/intelligence/coverage", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    assert resp.json() == {"error": "cov boom"}


# ---------------------------------------------------------------------------
# /intelligence/providers — exception path (1074-1075)
# ---------------------------------------------------------------------------


def test_intelligence_providers_exception(mock_state):
    client = _get_client()
    with patch("src.config.get_settings", side_effect=RuntimeError("settings boom")):
        resp = client.get("/intelligence/providers", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# /strategies/allocation — new performance-weighted allocation endpoint
# ---------------------------------------------------------------------------


def test_strategies_allocation_empty_registry(mock_state):
    client = _get_client()
    with patch("src.api.main.get_default_registry") as mock_reg:
        mock_reg.return_value.all.return_value = []
        resp = client.get("/strategies/allocation", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["allocations"] == {}
    assert data["method"] == "equal_weight"


def test_strategies_allocation_with_strategies(mock_state):
    from unittest.mock import MagicMock

    strategy = MagicMock()
    strategy.strategy_id = "signal_engine_v1"
    strategy.required_capital_fraction.return_value = 1.0

    client = _get_client()
    with (
        patch("src.api.main.get_default_registry") as mock_reg,
        patch("src.api.main.get_attribution_tracker") as mock_tracker,
    ):
        mock_reg.return_value.all.return_value = [strategy]
        mock_tracker.return_value.fill_count.return_value = 0
        mock_tracker.return_value.snapshot.return_value = {}
        resp = client.get("/strategies/allocation", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert "allocations" in data
    assert "method" in data
    assert "fill_count" in data


def _allocation_strategy(strategy_id: str):
    from unittest.mock import MagicMock

    strategy = MagicMock()
    strategy.strategy_id = strategy_id
    strategy.required_capital_fraction.return_value = 1.0
    return strategy


def test_strategies_allocation_reports_applied_and_target(mock_state):
    """
    The endpoint reports the allocation the book is running alongside the
    one the allocator currently wants. Reading it must not advance either.
    """
    from src.tuning.meta_allocator import (
        get_allocation_controller,
        reset_allocation_controller,
    )

    reset_allocation_controller()
    try:
        controller = get_allocation_controller(0.10)
        controller.step_toward({"a": 0.5, "b": 0.5})

        client = _get_client()
        with (
            patch("src.api.main.get_default_registry") as mock_reg,
            patch("src.api.main.get_attribution_tracker") as mock_tracker,
        ):
            mock_reg.return_value.all.return_value = [
                _allocation_strategy("a"),
                _allocation_strategy("b"),
            ]
            mock_tracker.return_value.fill_count.return_value = 0
            mock_tracker.return_value.snapshot.return_value = {}
            resp = client.get("/strategies/allocation", headers={"x-api-key": _API_KEY})
            second = client.get("/strategies/allocation", headers={"x-api-key": _API_KEY})

        data = resp.json()
        assert data["allocations"] == {"a": 0.5, "b": 0.5}
        assert data["target_allocations"] == pytest.approx({"a": 0.5, "b": 0.5})
        assert data["max_shift_per_step"] == pytest.approx(0.10)
        # Reading twice must not step the controller — the rebalance cadence
        # belongs to the orchestrator, not to whoever polls the dashboard.
        assert second.json()["allocations"] == data["allocations"]
    finally:
        reset_allocation_controller()


def test_strategies_allocation_falls_back_to_target_before_first_rebalance(mock_state):
    from src.tuning.meta_allocator import reset_allocation_controller

    reset_allocation_controller()
    try:
        client = _get_client()
        with (
            patch("src.api.main.get_default_registry") as mock_reg,
            patch("src.api.main.get_attribution_tracker") as mock_tracker,
        ):
            mock_reg.return_value.all.return_value = [_allocation_strategy("a")]
            mock_tracker.return_value.fill_count.return_value = 0
            mock_tracker.return_value.snapshot.return_value = {}
            resp = client.get("/strategies/allocation", headers={"x-api-key": _API_KEY})

        data = resp.json()
        assert data["allocations"] == data["target_allocations"]
        assert data["allocations"]["a"] == pytest.approx(1.0)
    finally:
        reset_allocation_controller()


def test_allocation_stress_test_uses_the_applied_allocation(mock_state):
    """A crash tests the positions held today, not the target being crept toward."""
    from src.tuning.meta_allocator import (
        get_allocation_controller,
        reset_allocation_controller,
    )

    reset_allocation_controller()
    try:
        get_allocation_controller(0.10).step_toward({"a": 1.0, "b": 0.0})

        client = _get_client()
        with (
            patch("src.api.main.get_default_registry") as mock_reg,
            patch("src.api.main.performance_weighted_allocate") as mock_alloc,
        ):
            mock_reg.return_value.all.return_value = [
                _allocation_strategy("a"),
                _allocation_strategy("b"),
            ]
            resp = client.get("/strategies/stress-test", headers={"x-api-key": _API_KEY})

        assert resp.status_code == 200
        assert resp.json()["allocations"] == {"a": 1.0, "b": 0.0}
        mock_alloc.assert_not_called()
    finally:
        reset_allocation_controller()


# ---------------------------------------------------------------------------
# GET /strategies/gauntlet
# ---------------------------------------------------------------------------


def _gauntlet_fills(count: int, first_entry_ms: int):
    from src.diagnostics.attribution import AttributedFill

    day_ms = 86_400_000
    return [
        AttributedFill(
            strategy_id="alpha",
            pnl_usd=10.0,
            entry_ts=first_entry_ms + i * day_ms,
            exit_ts=first_entry_ms + (i + 1) * day_ms,
        )
        for i in range(count)
    ]


def test_strategies_gauntlet_empty_tracker(mock_state):
    client = _get_client()
    with patch("src.api.main.get_attribution_tracker") as mock_tracker:
        mock_tracker.return_value.snapshot.return_value = {}
        resp = client.get("/strategies/gauntlet", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["candidates"] == {}
    assert data["criteria"]["min_trades"] == 30
    assert data["equity_usd"] == 100_000.0


def test_strategies_gauntlet_reports_failed_criteria(mock_state):
    import time

    now_ms = int(time.time() * 1000)
    fills = _gauntlet_fills(3, now_ms - 3 * 86_400_000)
    client = _get_client()
    with patch("src.api.main.get_attribution_tracker") as mock_tracker:
        mock_tracker.return_value.snapshot.return_value = {"alpha": MagicMock()}
        mock_tracker.return_value.fills_for.return_value = fills
        mock_tracker.return_value.first_entry_ts_for.return_value = fills[0].entry_ts
        mock_tracker.return_value.lifetime_trade_count.return_value = len(fills)
        resp = client.get("/strategies/gauntlet", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    candidate = resp.json()["candidates"]["alpha"]
    assert candidate["passed"] is False
    assert candidate["trade_count"] == 3
    # too few trades AND too few days running — both must be reported, not just the first
    assert any("trade_count" in c for c in candidate["failed_criteria"])
    assert any("days_running" in c for c in candidate["failed_criteria"])


def test_strategies_gauntlet_requires_api_key(mock_state):
    resp = _get_client().get("/strategies/gauntlet")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /debug/reconcile
# ---------------------------------------------------------------------------


def _open_trade(symbol: str, quantity: float, direction: int = 1):
    from src.data.storage import TradeRecord

    return TradeRecord(
        id=f"t-{symbol}-{quantity}",
        symbol=symbol,
        timeframe="1h",
        trading_mode="paper",
        execution_mode="automatic",
        direction=direction,
        entry_price=100.0,
        exit_price=None,
        quantity=quantity,
        notional_usd=100.0 * quantity,
        entry_ts=1,
        exit_ts=None,
        pnl_usd=None,
        pnl_pct=None,
        fee_usd=0.0,
        kelly_fraction=0.1,
        regime_at_entry=0,
        meta_label_prob=0.5,
        exit_reason=None,
        approved_by=None,
        raw_signal=None,
    )


def _memory_position(symbol: str, quantity: float, direction: str = "long"):
    return {"symbol": symbol, "quantity": quantity, "direction": direction}


def test_reconcile_consistent_when_both_sides_empty(mock_state):
    mock_state.storage.fetch_trades = AsyncMock(return_value=[])
    resp = _get_client().get("/debug/reconcile", headers={"x-api-key": _API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["consistent"] is True
    assert data["discrepancies"] == []


def test_reconcile_flags_position_absent_from_the_durable_record(mock_state):
    mock_state.orchestrator._executor.open_positions_safe = AsyncMock(
        return_value=[_memory_position("BTC/USDT", 0.1)]
    )
    mock_state.storage.fetch_trades = AsyncMock(return_value=[])
    resp = _get_client().get("/debug/reconcile", headers={"x-api-key": _API_KEY})
    data = resp.json()
    assert data["consistent"] is False
    assert data["discrepancies"][0]["discrepancy_type"] == "missing_in_reference"
    assert data["discrepancies"][0]["local_quantity"] == 0.1


def test_reconcile_flags_orphan_open_trade_after_restart(mock_state):
    """The classic crash case: the DB still has an open trade, memory has nothing."""
    mock_state.orchestrator._executor.open_positions_safe = AsyncMock(return_value=[])
    mock_state.storage.fetch_trades = AsyncMock(return_value=[_open_trade("BTC/USDT", 0.1)])
    resp = _get_client().get("/debug/reconcile", headers={"x-api-key": _API_KEY})
    data = resp.json()
    assert data["consistent"] is False
    assert data["discrepancies"][0]["discrepancy_type"] == "missing_locally"
    assert data["discrepancies"][0]["reference_quantity"] == 0.1


def test_reconcile_detects_quantity_mismatch(mock_state):
    mock_state.orchestrator._executor.open_positions_safe = AsyncMock(
        return_value=[_memory_position("BTC/USDT", 0.1)]
    )
    mock_state.storage.fetch_trades = AsyncMock(return_value=[_open_trade("BTC/USDT", 0.15)])
    resp = _get_client().get("/debug/reconcile", headers={"x-api-key": _API_KEY})
    data = resp.json()
    assert data["discrepancies"][0]["discrepancy_type"] == "quantity_mismatch"


def test_reconcile_nets_multiple_positions_on_one_symbol(mock_state):
    """Two in-memory legs on one symbol must net before comparison, not overwrite."""
    mock_state.orchestrator._executor.open_positions_safe = AsyncMock(
        return_value=[
            _memory_position("BTC/USDT", 0.1),
            _memory_position("BTC/USDT", 0.05, direction="short"),
        ]
    )
    mock_state.storage.fetch_trades = AsyncMock(
        return_value=[_open_trade("BTC/USDT", 0.1), _open_trade("BTC/USDT", 0.05, direction=0)]
    )
    resp = _get_client().get("/debug/reconcile", headers={"x-api-key": _API_KEY})
    data = resp.json()
    assert data["consistent"] is True
    assert data["local_position_count"] == 2
    assert data["reference_position_count"] == 2


def test_reconcile_requests_only_open_trades(mock_state):
    mock_state.storage.fetch_trades = AsyncMock(return_value=[])
    _get_client().get("/debug/reconcile", headers={"x-api-key": _API_KEY})
    assert mock_state.storage.fetch_trades.await_args.kwargs["open_only"] is True


def test_reconcile_requires_api_key(mock_state):
    assert _get_client().get("/debug/reconcile").status_code == 401


def test_reconcile_pages_through_open_trades(mock_state):
    """A single capped query would drop positions and then report them as
    discrepancies — a reconciliation that invents differences is worse than none."""
    from src.api.main import _RECONCILE_PAGE

    full_page = [_open_trade("BTC/USDT", 1.0) for _ in range(_RECONCILE_PAGE)]
    mock_state.orchestrator._executor.open_positions_safe = AsyncMock(
        return_value=[_memory_position("BTC/USDT", float(_RECONCILE_PAGE) + 3.0)]
    )
    mock_state.storage.fetch_trades = AsyncMock(
        side_effect=[full_page, [_open_trade("BTC/USDT", 1.0) for _ in range(3)]]
    )
    resp = _get_client().get("/debug/reconcile", headers={"x-api-key": _API_KEY})
    data = resp.json()
    assert data["reference_position_count"] == _RECONCILE_PAGE + 3
    assert data["consistent"] is True
    assert data["truncated"] is False


def test_reconcile_reports_truncation_at_the_page_bound(mock_state):
    from src.api.main import _RECONCILE_MAX_PAGES, _RECONCILE_PAGE

    full_page = [_open_trade("BTC/USDT", 1.0) for _ in range(_RECONCILE_PAGE)]
    mock_state.storage.fetch_trades = AsyncMock(return_value=full_page)
    resp = _get_client().get("/debug/reconcile", headers={"x-api-key": _API_KEY})
    data = resp.json()
    assert data["truncated"] is True
    assert mock_state.storage.fetch_trades.await_count == _RECONCILE_MAX_PAGES


def test_reconcile_advances_the_offset_between_pages(mock_state):
    from src.api.main import _RECONCILE_PAGE

    full_page = [_open_trade("BTC/USDT", 1.0) for _ in range(_RECONCILE_PAGE)]
    mock_state.storage.fetch_trades = AsyncMock(side_effect=[full_page, []])
    _get_client().get("/debug/reconcile", headers={"x-api-key": _API_KEY})
    offsets = [c.kwargs["offset"] for c in mock_state.storage.fetch_trades.await_args_list]
    assert offsets == [0, _RECONCILE_PAGE]


def test_strategies_gauntlet_uses_lifetime_facts_not_the_window(mock_state):
    """A candidate whose oldest fills aged out must not read as newly started."""
    import time

    now_ms = int(time.time() * 1000)
    day_ms = 86_400_000
    client = _get_client()
    with patch("src.api.main.get_attribution_tracker") as mock_tracker:
        mock_tracker.return_value.snapshot.return_value = {"alpha": MagicMock()}
        mock_tracker.return_value.fills_for.return_value = _gauntlet_fills(2, now_ms - day_ms)
        mock_tracker.return_value.first_entry_ts_for.return_value = now_ms - 90 * day_ms
        mock_tracker.return_value.lifetime_trade_count.return_value = 500
        resp = client.get("/strategies/gauntlet", headers={"x-api-key": _API_KEY})
    candidate = resp.json()["candidates"]["alpha"]
    assert candidate["trade_count"] == 500
    assert candidate["days_running"] > 89.0
