"""
Tests for the order FSM registry follow-up to GAP-004, and the two
endpoints (GET /orders/{order_id}/status, GET /performance-drift) whose
access paths were fixed during the 2026-06-25 audit.

Background: LiveExecutor previously discarded its OrderFSM state as a
local variable in _place_market_order() once the function returned --
nothing persisted it, despite the function's docstring claiming "state
persistence for manual reconciliation". GET /orders/{order_id}/status
also read a nonexistent `runtime_config.executor` attribute (and
/performance-drift read a nonexistent `runtime_config.drift_adapter`),
so both endpoints always fell through to their "not found"/error branch
regardless of what LiveExecutor exposed.

These tests cover:
  - LiveExecutor._register_order_fsm() / get_order_fsm_state() in
    isolation, via object.__new__ to avoid LiveExecutor.__init__'s
    TRADING_MODE=live gate (get_settings() is lru_cache'd process-wide,
    so flipping the env var mid-test-session is unsafe -- this avoids
    that entirely).
  - The bounded eviction behavior (_ORDER_FSM_REGISTRY_MAX_SIZE).
  - The two API endpoints, via the same TestClient pattern introduced in
    tests/test_risk_controls_api.py.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Any

import pytest

from src.execution.live import (
    _ORDER_FSM_REGISTRY_MAX_SIZE,
    LiveExecutor,
)
from src.execution.order_fsm import OrderFSM, OrderFSMState, OrderStatus


def _make_fsm(order_id: str) -> OrderFSM:
    state = OrderFSMState(
        order_id=order_id,
        symbol="BTC/USDT",
        side="buy",
        quantity=1.0,
        status=OrderStatus.FILLED,
        filled_qty=1.0,
        average_fill_price=65000.0,
    )
    return OrderFSM(state)


def _make_bare_executor() -> LiveExecutor:
    """
    Construct a LiveExecutor instance without running __init__ (which
    requires TRADING_MODE=live and real storage/fetcher dependencies),
    setting only the one attribute the registry methods need.
    """
    executor = object.__new__(LiveExecutor)
    executor._order_fsm_registry = OrderedDict()
    return executor


class TestOrderFSMRegistry:
    def test_register_then_lookup_returns_state(self) -> None:
        executor = _make_bare_executor()
        fsm = _make_fsm("order-1")
        executor._register_order_fsm(fsm)

        import asyncio

        result = asyncio.run(executor.get_order_fsm_state("order-1"))
        assert result is not None
        assert result.order_id == "order-1"
        assert result.status == OrderStatus.FILLED

    def test_lookup_unknown_order_id_returns_none(self) -> None:
        executor = _make_bare_executor()
        import asyncio

        result = asyncio.run(executor.get_order_fsm_state("never-placed"))
        assert result is None

    def test_registry_evicts_oldest_beyond_max_size(self) -> None:
        executor = _make_bare_executor()
        for i in range(_ORDER_FSM_REGISTRY_MAX_SIZE + 10):
            executor._register_order_fsm(_make_fsm(f"order-{i}"))

        assert len(executor._order_fsm_registry) == _ORDER_FSM_REGISTRY_MAX_SIZE
        # The earliest orders should have been evicted (oldest-first).
        assert "order-0" not in executor._order_fsm_registry
        assert "order-9" not in executor._order_fsm_registry
        # The most recent ones should still be present.
        last_id = f"order-{_ORDER_FSM_REGISTRY_MAX_SIZE + 9}"
        assert last_id in executor._order_fsm_registry

    def test_re_registering_same_order_id_moves_to_end(self) -> None:
        """Re-registering an existing order_id (e.g. updated state) should
        refresh its position in eviction order, not create a duplicate."""
        executor = _make_bare_executor()
        executor._register_order_fsm(_make_fsm("order-A"))
        for i in range(5):
            executor._register_order_fsm(_make_fsm(f"order-{i}"))
        # Re-register order-A -- it should now be the most recent, not evicted
        # first even if many more orders come in after this point.
        executor._register_order_fsm(_make_fsm("order-A"))

        assert len(executor._order_fsm_registry) == 6  # no duplicate entry
        # order-A should be last (most recently touched).
        assert list(executor._order_fsm_registry.keys())[-1] == "order-A"


@pytest.fixture()
def api_client():
    os.environ["API_SECRET_KEY"] = "test-key-" + "a" * 32
    os.environ["OPERATOR_SECRET"] = "test-operator-secret"

    from fastapi.testclient import TestClient

    from src.api import main as api_main

    class _FakeExecutor:
        def __init__(self) -> None:
            self._states: dict[str, Any] = {}

        async def get_order_fsm_state(self, order_id: str):
            return self._states.get(order_id)

    class _FakeDriftAdapter:
        def __init__(self, result: dict[str, Any]) -> None:
            self._result = result

        def check_drift(self) -> dict[str, Any]:
            return self._result

    class _FakeOrchestrator:
        def __init__(self) -> None:
            self._executor = _FakeExecutor()
            self._drift_adapter = _FakeDriftAdapter(
                {"drifted": False, "reason": "ok", "metrics": {}}
            )

    api_main._state.ready = True
    api_main._state.orchestrator = _FakeOrchestrator()
    api_main.app.dependency_overrides[api_main.api_key_header] = lambda: None
    api_main.app.dependency_overrides[api_main.require_ready] = lambda: None

    client = TestClient(api_main.app)
    yield client, api_main

    api_main.app.dependency_overrides.clear()


class TestOrderStatusEndpoint:
    def test_unknown_order_returns_helpful_error_not_crash(self, api_client) -> None:
        client, _main = api_client
        resp = client.get("/orders/does-not-exist/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "error" in body

    def test_known_order_returns_fsm_snapshot(self, api_client) -> None:
        client, api_main = api_client
        fsm_state = OrderFSMState(
            order_id="abc123",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            status=OrderStatus.FILLED,
            filled_qty=1.0,
            average_fill_price=65000.0,
        )
        api_main._state.orchestrator._executor._states["abc123"] = fsm_state

        resp = client.get("/orders/abc123/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["order_id"] == "abc123"
        assert body["status"] == "filled"
        assert body["filled_qty"] == 1.0


class TestPerformanceDriftEndpoint:
    def test_returns_drift_adapter_result(self, api_client) -> None:
        client, _main = api_client
        resp = client.get("/performance-drift")
        assert resp.status_code == 200
        body = resp.json()
        assert body["drifted"] is False
        assert body["reason"] == "ok"
