"""
Tests for Phase 6: GET/POST /self-tuning/* API endpoints.

Follows the same TestClient harness pattern established in
tests/test_risk_controls_api.py.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from src.tuning.registry import TunableParameter


_TEST_SECRET = "test-operator-secret"  # pragma: allowlist secret
_WRONG_SECRET = "wrong-secret"  # pragma: allowlist secret


@pytest.fixture()
def api_client():
    os.environ["API_SECRET_KEY"] = "test-key-" + "a" * 32  # pragma: allowlist secret
    os.environ["OPERATOR_SECRET"] = _TEST_SECRET

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

    # The tuning singletons (src/tuning/state.py) are process-wide by design
    # (mirrors runtime_config) -- reset their in-memory state between tests
    # so one test's promotions/registrations don't leak into the next.
    api_main.tuning_version_store._history.clear()  # -- test isolation reset
    if api_main.tuning_version_store.path.exists():
        api_main.tuning_version_store.path.write_text("")
    api_main.tuning_registry._params.clear()  # -- test isolation reset
    api_main.tuning_pause_state._paused = False  # -- test isolation reset

    api_main.app.dependency_overrides[api_main.api_key_header] = lambda: None
    # The mutating endpoints now also depend on resolve_role (RBAC).
    # Overriding api_key_header alone leaves that dependency live and
    # every POST answers 401 with no API_SECRET_KEY configured.
    api_main.app.dependency_overrides[api_main.resolve_role] = lambda: (
        api_main.Role.TRADE_AUTHORIZING
    )
    api_main.app.dependency_overrides[api_main.require_ready] = lambda: None

    client = TestClient(api_main.app)
    yield client, fake_storage, api_main

    api_main.app.dependency_overrides.clear()


def _register_test_param(api_main) -> None:
    if not api_main.tuning_registry.is_registered("hmm.entropy_threshold"):
        api_main.tuning_registry.register(
            TunableParameter(
                name="hmm.entropy_threshold",
                description="test param",
                floor=0.3,
                ceiling=0.7,
                current=0.5,
                eval_strategy="cpcv_oos_sharpe",
            )
        )


class TestSelfTuningStatus:
    def test_status_returns_shape(self, api_client) -> None:
        client, _storage, api_main = api_client
        _register_test_param(api_main)
        resp = client.get("/self-tuning/status")
        assert resp.status_code == 200
        body = resp.json()
        assert "enabled" in body
        assert "shadow_mode" in body
        assert "paused" in body
        assert any(p["name"] == "hmm.entropy_threshold" for p in body["parameters"])

    def test_status_does_not_require_operator_secret(self, api_client) -> None:
        client, _storage, _main = api_client
        resp = client.get("/self-tuning/status")
        assert resp.status_code == 200

    def test_status_includes_current_version_when_promoted(self, api_client) -> None:
        client, _storage, api_main = api_client
        _register_test_param(api_main)
        api_main.tuning_version_store.promote(
            "hmm.entropy_threshold", 0.55, evidence={}, promoted_by="alice"
        )
        resp = client.get("/self-tuning/status")
        assert resp.status_code == 200
        body = resp.json()
        param = next(p for p in body["parameters"] if p["name"] == "hmm.entropy_threshold")
        assert param["current_version"] is not None
        assert param["current_version"]["value"] == pytest.approx(0.55)
        assert param["current_version"]["promoted_by"] == "alice"


class TestSelfTuningPauseResume:
    def test_pause_requires_operator_secret(self, api_client) -> None:
        client, _storage, _main = api_client
        resp = client.post(
            "/self-tuning/pause", json={"operator": "alice", "operator_secret": _WRONG_SECRET}
        )
        assert resp.status_code == 401

    def test_pause_returns_503_when_operator_secret_not_configured(self, api_client) -> None:
        client, _storage, _main = api_client
        del os.environ["OPERATOR_SECRET"]
        try:
            resp = client.post(
                "/self-tuning/pause", json={"operator": "alice", "operator_secret": _TEST_SECRET}
            )
        finally:
            os.environ["OPERATOR_SECRET"] = _TEST_SECRET
        assert resp.status_code == 503

    def test_pause_then_resume_round_trip(self, api_client) -> None:
        client, _storage, api_main = api_client
        resp = client.post(
            "/self-tuning/pause", json={"operator": "alice", "operator_secret": _TEST_SECRET}
        )
        assert resp.status_code == 200
        assert resp.json()["paused"] is True
        assert api_main.tuning_pause_state._paused is True

        resp2 = client.post(
            "/self-tuning/resume", json={"operator": "alice", "operator_secret": _TEST_SECRET}
        )
        assert resp2.status_code == 200
        assert resp2.json()["paused"] is False
        assert api_main.tuning_pause_state._paused is False

    def test_pause_records_audit_event(self, api_client) -> None:
        client, _storage, api_main = api_client
        client.post(
            "/self-tuning/pause", json={"operator": "alice", "operator_secret": _TEST_SECRET}
        )
        events = api_main.tuning_audit_log.read_for_param("__global__")
        assert any(e.event_type.value == "paused" for e in events)


class TestSelfTuningRollback:
    def test_rollback_requires_operator_secret(self, api_client) -> None:
        client, _storage, _main = api_client
        resp = client.post(
            "/self-tuning/rollback/hmm.entropy_threshold",
            json={"operator": "alice", "operator_secret": _WRONG_SECRET},
        )
        assert resp.status_code == 401

    def test_rollback_with_no_history_returns_404(self, api_client) -> None:
        client, _storage, _api_main = api_client
        resp = client.post(
            "/self-tuning/rollback/does.not.exist",
            json={"operator": "alice", "operator_secret": _TEST_SECRET},
        )
        assert resp.status_code == 404

    def test_rollback_with_one_version_returns_404(self, api_client) -> None:
        client, _storage, api_main = api_client
        api_main.tuning_version_store.promote("hmm.entropy_threshold", 0.55, {})
        resp = client.post(
            "/self-tuning/rollback/hmm.entropy_threshold",
            json={"operator": "alice", "operator_secret": _TEST_SECRET},
        )
        assert resp.status_code == 404

    def test_rollback_with_two_versions_reverts(self, api_client) -> None:
        client, _storage, api_main = api_client
        api_main.tuning_version_store.promote("hmm.entropy_threshold", 0.50, {})
        api_main.tuning_version_store.promote("hmm.entropy_threshold", 0.65, {})

        resp = client.post(
            "/self-tuning/rollback/hmm.entropy_threshold",
            json={"operator": "alice", "operator_secret": _TEST_SECRET},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["reverted_value"] == 0.50
        assert api_main.tuning_version_store.current("hmm.entropy_threshold").value == 0.50

        events = api_main.tuning_audit_log.read_for_param("hmm.entropy_threshold")
        assert any(e.event_type.value == "rolled_back" for e in events)
