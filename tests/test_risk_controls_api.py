"""
Tests for GAP-013: runtime-toggleable position-exit controls.

Covers:
  - check_position_exit() pure decision logic (src/risk/gates.py)
  - RuntimeConfig.get_risk_controls() / set_risk_controls() (src/config.py)
  - GET/POST /risk-controls API endpoints (src/api/main.py), via a minimal
    FastAPI TestClient harness -- no such harness existed in this repo
    before this file; built here following the dependency-override pattern
    so tests don't need a real orchestrator/storage backend.

NOTE: this also exercises Gap-014's fix (src/api/main.py was previously
unimportable under the installed fastapi/pydantic versions). If this file
fails to collect with an AssertionError or NameError at import time, that
regression has reappeared -- see .project-intel/GAPS.md Gap-014.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from src.config import RuntimeConfig, get_settings
from src.risk.gates import check_position_exit


# ---------------------------------------------------------------------------
# Pure logic: check_position_exit
# ---------------------------------------------------------------------------

_NOW_MS = 1_700_000_000_000


class TestCheckPositionExit:
    def test_stop_loss_triggers(self) -> None:
        reason = check_position_exit(
            unrealized_pnl_pct=-2.5,
            entry_ts_ms=_NOW_MS - 1_000,
            now_ts_ms=_NOW_MS,
            stop_loss_enabled=True,
            stop_loss_pct=2.0,
            take_profit_enabled=True,
            take_profit_pct=4.0,
            max_holding_period_s=86400.0,
        )
        assert reason == "stop_loss"

    def test_stop_loss_exact_boundary_triggers(self) -> None:
        # <=, not <, so exactly -2.0% on a 2.0% threshold should trigger.
        reason = check_position_exit(
            unrealized_pnl_pct=-2.0,
            entry_ts_ms=_NOW_MS - 1_000,
            now_ts_ms=_NOW_MS,
            stop_loss_enabled=True,
            stop_loss_pct=2.0,
            take_profit_enabled=True,
            take_profit_pct=4.0,
            max_holding_period_s=86400.0,
        )
        assert reason == "stop_loss"

    def test_take_profit_triggers(self) -> None:
        reason = check_position_exit(
            unrealized_pnl_pct=4.5,
            entry_ts_ms=_NOW_MS - 1_000,
            now_ts_ms=_NOW_MS,
            stop_loss_enabled=True,
            stop_loss_pct=2.0,
            take_profit_enabled=True,
            take_profit_pct=4.0,
            max_holding_period_s=86400.0,
        )
        assert reason == "profit_target"

    def test_stop_loss_disabled_does_not_trigger(self) -> None:
        reason = check_position_exit(
            unrealized_pnl_pct=-10.0,
            entry_ts_ms=_NOW_MS - 1_000,
            now_ts_ms=_NOW_MS,
            stop_loss_enabled=False,
            stop_loss_pct=2.0,
            take_profit_enabled=True,
            take_profit_pct=4.0,
            max_holding_period_s=86400.0,
        )
        assert reason is None

    def test_take_profit_disabled_does_not_trigger(self) -> None:
        reason = check_position_exit(
            unrealized_pnl_pct=50.0,
            entry_ts_ms=_NOW_MS - 1_000,
            now_ts_ms=_NOW_MS,
            stop_loss_enabled=True,
            stop_loss_pct=2.0,
            take_profit_enabled=False,
            take_profit_pct=4.0,
            max_holding_period_s=86400.0,
        )
        assert reason is None

    def test_time_exit_triggers_regardless_of_toggles(self) -> None:
        # Time exit has no runtime toggle -- always enforced.
        reason = check_position_exit(
            unrealized_pnl_pct=0.5,
            entry_ts_ms=_NOW_MS - 90_000_000,
            now_ts_ms=_NOW_MS,
            stop_loss_enabled=False,
            stop_loss_pct=2.0,
            take_profit_enabled=False,
            take_profit_pct=4.0,
            max_holding_period_s=86400.0,
        )
        assert reason == "time_exit"

    def test_no_trigger_when_within_all_bounds(self) -> None:
        reason = check_position_exit(
            unrealized_pnl_pct=1.0,
            entry_ts_ms=_NOW_MS - 1_000,
            now_ts_ms=_NOW_MS,
            stop_loss_enabled=True,
            stop_loss_pct=2.0,
            take_profit_enabled=True,
            take_profit_pct=4.0,
            max_holding_period_s=86400.0,
        )
        assert reason is None

    def test_stop_loss_checked_before_take_profit_on_simultaneous_trigger(self) -> None:
        # Can't normally hit both at once (pnl can't be both <=-2 and >=4),
        # but verify documented precedence holds for the boundary case
        # where both toggles are on and pnl satisfies neither boundary
        # incorrectly -- this is really just re-confirming ordering doesn't
        # accidentally let take-profit shadow stop-loss.
        reason = check_position_exit(
            unrealized_pnl_pct=-5.0,
            entry_ts_ms=_NOW_MS - 1_000,
            now_ts_ms=_NOW_MS,
            stop_loss_enabled=True,
            stop_loss_pct=2.0,
            take_profit_enabled=True,
            take_profit_pct=4.0,
            max_holding_period_s=86400.0,
        )
        assert reason == "stop_loss"

    def test_negative_pct_inputs_normalized_via_abs(self) -> None:
        # stop_loss_pct/take_profit_pct should be treated as magnitudes
        # even if a caller mistakenly passes a negative stop_loss_pct.
        reason = check_position_exit(
            unrealized_pnl_pct=-3.0,
            entry_ts_ms=_NOW_MS - 1_000,
            now_ts_ms=_NOW_MS,
            stop_loss_enabled=True,
            stop_loss_pct=-2.0,
            take_profit_enabled=True,
            take_profit_pct=4.0,
            max_holding_period_s=86400.0,
        )
        assert reason == "stop_loss"


# ---------------------------------------------------------------------------
# RuntimeConfig.get_risk_controls / set_risk_controls
# ---------------------------------------------------------------------------


class TestRuntimeConfigRiskControls:
    def test_seeded_from_settings_defaults(self) -> None:
        async def _run() -> dict[str, Any]:
            rc = RuntimeConfig()
            return await rc.get_risk_controls()

        snap = asyncio.run(_run())
        cfg = get_settings()
        assert snap["stop_loss_enabled"] == cfg.risk.stop_loss_enabled_default
        assert snap["stop_loss_pct"] == cfg.risk.stop_loss_pct_default
        assert snap["take_profit_enabled"] == cfg.risk.take_profit_enabled_default
        assert snap["take_profit_pct"] == cfg.risk.take_profit_pct_default
        assert snap["max_holding_period_s"] == cfg.risk.max_holding_period_s_default

    def test_partial_update_leaves_other_fields_unchanged(self) -> None:
        async def _run() -> dict[str, Any]:
            rc = RuntimeConfig()
            before = await rc.get_risk_controls()
            after = await rc.set_risk_controls(stop_loss_pct=3.3)
            return before, after

        before, after = asyncio.run(_run())
        assert after["stop_loss_pct"] == 3.3
        assert after["take_profit_pct"] == before["take_profit_pct"]
        assert after["stop_loss_enabled"] == before["stop_loss_enabled"]
        assert after["take_profit_enabled"] == before["take_profit_enabled"]
        assert after["max_holding_period_s"] == before["max_holding_period_s"]

    def test_toggle_disable_then_enable(self) -> None:
        async def _run() -> tuple[bool, bool]:
            rc = RuntimeConfig()
            await rc.set_risk_controls(stop_loss_enabled=False)
            mid = (await rc.get_risk_controls())["stop_loss_enabled"]
            await rc.set_risk_controls(stop_loss_enabled=True)
            end = (await rc.get_risk_controls())["stop_loss_enabled"]
            return mid, end

        mid, end = asyncio.run(_run())
        assert mid is False
        assert end is True

    def test_concurrent_updates_serialize_without_torn_state(self) -> None:
        """Many concurrent set_risk_controls calls should never leave a
        half-applied state -- each call holds the lock for its full update."""

        async def _run() -> dict[str, Any]:
            rc = RuntimeConfig()

            async def _bump(i: int) -> None:
                await rc.set_risk_controls(stop_loss_pct=1.0 + i, take_profit_pct=2.0 + i)

            await asyncio.gather(*[_bump(i) for i in range(20)])
            return await rc.get_risk_controls()

        snap = asyncio.run(_run())
        # Whichever write landed last, stop_loss_pct and take_profit_pct
        # must be from the SAME call (i.e. take_profit_pct - stop_loss_pct == 1.0),
        # never a mix of two different concurrent calls' values.
        assert snap["take_profit_pct"] - snap["stop_loss_pct"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# API endpoints: GET/POST /risk-controls
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_client():
    """
    Minimal TestClient harness for src/api/main.py.

    No TestClient fixture existed anywhere in this repo before this file
    (see Debt-005 / Gap-014) -- src/api/main.py keeps almost all app state
    in a module-level `_state` singleton populated by the real lifespan(),
    so for isolated endpoint tests we bypass lifespan entirely:
      - require_ready / api_key_header are overridden via
        app.dependency_overrides (FastAPI's supported mechanism for this).
      - _state.storage is monkeypatched to a tiny fake that only implements
        insert_audit_event (the only storage method these endpoints call).
      - OPERATOR_SECRET is set via env var, matching how the real
        set_execution_mode/set_risk_controls endpoints read it.
    """
    os.environ["API_SECRET_KEY"] = "test-key-" + "a" * 32
    os.environ["OPERATOR_SECRET"] = "test-operator-secret"

    from fastapi.testclient import TestClient

    from src.api import main as api_main

    class _FakeStorage:
        def __init__(self) -> None:
            self.audit_events: list[dict[str, Any]] = []

        async def insert_audit_event(
            self, event_type: str, operator: str, details: dict[str, Any] | None = None
        ) -> None:
            self.audit_events.append(
                {"event_type": event_type, "operator": operator, "details": details}
            )

    fake_storage = _FakeStorage()
    api_main._state.storage = fake_storage  # type: ignore[assignment]
    api_main._state.ready = True

    api_main.app.dependency_overrides[api_main.api_key_header] = lambda: None
    api_main.app.dependency_overrides[api_main.require_ready] = lambda: None

    client = TestClient(api_main.app)
    yield client, fake_storage, api_main

    api_main.app.dependency_overrides.clear()


class TestRiskControlsEndpoints:
    def test_get_risk_controls_returns_defaults(self, api_client) -> None:
        client, _storage, _main = api_client
        resp = client.get("/risk-controls")
        assert resp.status_code == 200
        body = resp.json()
        assert "risk_controls" in body
        assert "stop_loss_enabled" in body["risk_controls"]
        assert "take_profit_pct" in body["risk_controls"]

    def test_post_risk_controls_requires_operator_secret(self, api_client) -> None:
        client, _storage, _main = api_client
        resp = client.post(
            "/risk-controls",
            json={
                "stop_loss_enabled": False,
                "operator": "alice",
                "operator_secret": "wrong-secret",
            },
        )
        assert resp.status_code == 401

    def test_post_risk_controls_with_correct_secret_updates_state(self, api_client) -> None:
        client, storage, _main = api_client
        resp = client.post(
            "/risk-controls",
            json={
                "stop_loss_enabled": False,
                "stop_loss_pct": 5.0,
                "operator": "alice",
                "operator_secret": "test-operator-secret",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["risk_controls"]["stop_loss_enabled"] is False
        assert body["risk_controls"]["stop_loss_pct"] == 5.0
        assert body["operator"] == "alice"

        # GET should now reflect the change.
        resp2 = client.get("/risk-controls")
        assert resp2.json()["risk_controls"]["stop_loss_enabled"] is False

        # An audit event should have been recorded.
        assert len(storage.audit_events) == 1
        assert storage.audit_events[0]["event_type"] == "risk_controls_change"
        assert storage.audit_events[0]["operator"] == "alice"

    def test_post_risk_controls_partial_update_only_changes_supplied_fields(
        self, api_client
    ) -> None:
        client, _storage, _main = api_client
        before = client.get("/risk-controls").json()["risk_controls"]

        resp = client.post(
            "/risk-controls",
            json={
                "take_profit_pct": 7.5,
                "operator": "bob",
                "operator_secret": "test-operator-secret",
            },
        )
        assert resp.status_code == 200
        after = resp.json()["risk_controls"]
        assert after["take_profit_pct"] == 7.5
        assert after["stop_loss_enabled"] == before["stop_loss_enabled"]
        assert after["stop_loss_pct"] == before["stop_loss_pct"]

    def test_post_risk_controls_rejects_out_of_range_value(self, api_client) -> None:
        client, _storage, _main = api_client
        resp = client.post(
            "/risk-controls",
            json={
                "stop_loss_pct": 999.0,  # exceeds le=50.0
                "operator": "alice",
                "operator_secret": "test-operator-secret",
            },
        )
        assert resp.status_code == 422

    def test_post_risk_controls_rejects_invalid_operator_name(self, api_client) -> None:
        client, _storage, _main = api_client
        resp = client.post(
            "/risk-controls",
            json={
                "stop_loss_enabled": True,
                "operator": "not a valid operator name!!",
                "operator_secret": "test-operator-secret",
            },
        )
        assert resp.status_code == 422

    def test_get_risk_controls_does_not_require_operator_secret(self, api_client) -> None:
        # Read-only endpoint -- only api_key_header (overridden in fixture),
        # no operator_secret needed, unlike the POST endpoint.
        client, _storage, _main = api_client
        resp = client.get("/risk-controls")
        assert resp.status_code == 200
