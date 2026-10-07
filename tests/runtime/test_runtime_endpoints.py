"""RES-017: the runtime state is queryable through the existing API, every
mutation needs CHANGE_RUNTIME plus the operator second factor and goes through
the change manager, and an AI client can request but never approve.

Decides: RES-017, RES-018"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.agent_control.model import (
    SCHEMA_VERSION,
    CompletionState,
    TaskPhase,
    TaskRun,
    TaskScope,
)
from src.agent_control.store import STATE_DIR_ENV, TaskStore
from src.api import runtime_control
from src.api.access_control import Role
from src.api.runtime_control import agent_ledgers
from src.eventbus import EventBus
from src.runtime.adapters import Discovery
from src.runtime.contracts import ComponentType, LifecycleState
from src.runtime.platform import RuntimePlatform, build_runtime_platform

from ._support import FakeController, spec

S = LifecycleState
TRADE = {"x-api-key": "t" * 32}
READ = {"x-api-key": "r" * 32}
SECRET = "s" * 32
HUMAN = {"operator": "alice", "operator_secret": SECRET}
AI = {"operator": "agent", "operator_secret": SECRET, "actor_kind": "ai"}


def _platform() -> RuntimePlatform:
    return build_runtime_platform(
        bus=EventBus(),
        discoveries=[
            Discovery(spec("w"), S.ACTIVE),
            Discovery(spec("v"), S.STANDBY),
            Discovery(spec("m", ComponentType.MODEL), S.STANDBY),
        ],
        controllers={t: FakeController() for t in ComponentType},
    )


Api = tuple[TestClient, RuntimePlatform]


@pytest.fixture
def api() -> Iterator[Api]:
    from src.api.main import AppState, app

    state = AppState()
    state.ready = True
    state.storage = AsyncMock()
    state.orchestrator = MagicMock()
    state.runtime = _platform()
    env = {
        "API_SECRET_KEY": "t" * 32,
        "API_READONLY_KEY": "r" * 32,
        "OPERATOR_SECRET": SECRET,
    }
    with patch.dict(os.environ, env), patch("src.api.main._state", state):
        yield TestClient(app, raise_server_exceptions=False), state.runtime


def test_reads_are_open_to_a_read_only_key(api: Api) -> None:
    client, _ = api
    overview = client.get("/runtime", headers=READ).json()
    assert overview["components"] == 3 and overview["by_state"]["STANDBY"] == 2
    models = client.get("/runtime/components?component_type=model", headers=READ).json()
    assert [c["component_id"] for c in models] == ["model:m"]
    bad_type = client.get("/runtime/components?component_type=nope", headers=READ)
    assert bad_type.status_code == 422
    one = client.get("/runtime/components/worker:w", headers=READ).json()
    assert one["state"] == "ACTIVE" and one["history"][0]["action"] == "DISCOVER"
    assert client.get("/runtime/components/worker:x", headers=READ).status_code == 404
    deps = client.get("/runtime/dependencies", headers=READ).json()
    assert deps["cycle"] is None and len(deps["order"]) == 3
    assert client.get("/runtime/changes", headers=READ).json() == []
    assert client.get("/runtime/changes/chg-x", headers=READ).status_code == 404
    assert client.get("/runtime/traces", headers=READ).json() == []
    assert client.get("/runtime/traces/t1", headers=READ).status_code == 404


def test_a_recorded_trace_is_served(api: Api) -> None:
    from src.eventbus import Event

    client, platform = api
    platform.traces.record(Event("signal", {"tradeable": False}, 1, context={"trace_id": "t1"}))
    body = client.get("/runtime/traces/t1", headers=READ).json()
    assert body["final_decision"] == "NO_TRADE"


def test_a_read_only_key_cannot_mutate(api: Api) -> None:
    client, platform = api
    body = {**HUMAN, "reason": "x"}
    resp = client.post("/runtime/components/worker:w/drain", headers=READ, json=body)
    assert resp.status_code == 403
    assert platform.registry.get("worker:w").state is S.ACTIVE


def test_the_operator_second_factor_is_required(api: Api) -> None:
    client, _ = api
    body = {"operator": "alice", "operator_secret": "wrong", "reason": "x"}
    wrong = client.post("/runtime/components/worker:w/drain", headers=TRADE, json=body)
    assert wrong.status_code == 401
    with patch.dict(os.environ, {"OPERATOR_SECRET": ""}):
        resp = client.post("/runtime/components/worker:w/drain", headers=TRADE, json=body)
    assert resp.status_code == 503


def test_taking_risk_off_executes_at_once_and_is_audited(api: Api) -> None:
    client, platform = api
    body = {**AI, "reason": "x"}
    resp = client.post("/runtime/components/worker:w/drain", headers=TRADE, json=body)
    change = resp.json()
    assert (resp.status_code, change["status"]) == (200, "PROMOTED")
    assert platform.registry.get("worker:w").state is S.DRAINED
    detail = client.get(f"/runtime/changes/{change['change_id']}", headers=READ).json()
    assert [a["event"] for a in detail["audit"]] == ["submitted:APPROVED", "executed", "promoted"]
    bad = client.post("/runtime/components/worker:w/replace", headers=TRADE, json=body)
    assert bad.status_code == 422


def test_an_ai_request_waits_for_a_human(api: Api) -> None:
    client, platform = api
    body = {**AI, "component_id": "worker:v", "action": "activate", "reason": "go live"}
    change = client.post("/runtime/changes", headers=TRADE, json=body).json()
    assert change["status"] == "AWAITING_APPROVAL"
    cid = change["change_id"]
    refused = client.post(f"/runtime/changes/{cid}/approve", headers=TRADE, json=AI)
    assert refused.status_code == 403
    assert platform.registry.get("worker:v").state is S.STANDBY
    approved = client.post(f"/runtime/changes/{cid}/approve", headers=TRADE, json=HUMAN).json()
    assert approved["status"] == "EXECUTED" and approved["approved_by"] == "alice"
    promoted = client.post(f"/runtime/changes/{cid}/promote", headers=TRADE, json=HUMAN).json()
    assert promoted["status"] == "PROMOTED"
    late = client.post(f"/runtime/changes/{cid}/cancel", headers=TRADE, json=HUMAN)
    assert late.status_code == 409
    odd = client.post(f"/runtime/changes/{cid}/frobnicate", headers=TRADE, json=HUMAN)
    assert odd.status_code == 404
    gone = client.post("/runtime/changes/chg-x/execute", headers=TRADE, json=HUMAN)
    assert gone.status_code == 404


def test_a_staged_class_is_approved_but_not_executed(api: Api) -> None:
    client, platform = api
    body = {**HUMAN, "component_id": "model:m", "action": "ACTIVATE", "reason": "promote"}
    cid = client.post("/runtime/changes", headers=TRADE, json=body).json()["change_id"]
    approved = client.post(f"/runtime/changes/{cid}/approve", headers=TRADE, json=HUMAN).json()
    assert approved["status"] == "APPROVED"
    assert platform.registry.get("model:m").state is S.STANDBY
    early = client.post(f"/runtime/changes/{cid}/execute", headers=TRADE, json=HUMAN)
    assert early.status_code == 409 and "shadow" in early.json()["detail"]


def test_version_changes_and_bad_requests(api: Api) -> None:
    client, platform = api
    base = {**HUMAN, "component_id": "worker:w", "reason": "upgrade"}
    ok = client.post(
        "/runtime/changes",
        headers=TRADE,
        json={**base, "action": "replace", "target_version": "2", "target_configuration": {"k": 1}},
    ).json()
    assert ok["status"] == "AWAITING_APPROVAL" and ok["new_version"] == "2"
    client.post(f"/runtime/changes/{ok['change_id']}/cancel", headers=TRADE, json=HUMAN)
    rolled = client.post(
        f"/runtime/changes/{ok['change_id']}/rollback", headers=TRADE, json={**HUMAN, "reason": ""}
    )
    assert rolled.status_code == 409
    zap = client.post("/runtime/changes", headers=TRADE, json={**base, "action": "zap"})
    assert zap.status_code == 422
    replace = {**base, "action": "replace", "target_version": ""}
    assert client.post("/runtime/changes", headers=TRADE, json=replace).status_code == 422
    unknown = {**replace, "component_id": "worker:none", "target_version": "2"}
    assert client.post("/runtime/changes", headers=TRADE, json=unknown).status_code == 404
    assert platform.registry.get("worker:w").version.version == "1"


def test_desired_state_and_reconcile(api: Api) -> None:
    client, platform = api
    body = {**HUMAN, "component_id": "worker:w", "target_state": "drained", "reason": "maint"}
    desired = client.post("/runtime/desired", headers=TRADE, json=body).json()
    assert desired["target_state"] == "DRAINED"
    passes = client.post("/runtime/reconcile", headers=TRADE, json=HUMAN).json()
    assert passes[0]["outcome"] == "executed: PROMOTED"
    assert platform.registry.get("worker:w").state is S.DRAINED
    elsewhere = {**body, "component_id": "worker:x"}
    missing = client.post("/runtime/desired", headers=TRADE, json=elsewhere)
    assert missing.status_code == 404
    state = client.post("/runtime/desired", headers=TRADE, json={**body, "target_state": "zzz"})
    assert state.status_code == 422
    version = client.post("/runtime/desired", headers=TRADE, json={**body, "target_version": "9"})
    assert version.status_code == 422


def test_routes_answer_503_without_a_platform(api: Api) -> None:
    client, _ = api
    from src.api import main

    main._state.runtime = None
    assert client.get("/runtime", headers=READ).status_code == 503


def test_runtime_topic_is_operator_only() -> None:
    from src.api.main import AppState

    state = AppState()
    assert "runtime" not in state.permitted_topics(Role.READ_ONLY)
    assert "runtime" in state.permitted_topics(Role.TRADE_AUTHORIZING)


async def test_start_runtime_platform_restores_and_fails_closed() -> None:
    from src.api import main

    state = main.AppState()
    state.storage = AsyncMock()
    state.storage.fetch_runtime_desired_states.return_value = {
        "worker:gone": {"target_state": "STOPPED"}
    }
    with patch("src.api.main._state", state):
        platform = await main.start_runtime_platform()
        assert platform is not None
        with patch("src.api.main.build_runtime_platform", side_effect=ValueError("dup")):
            assert await main.start_runtime_platform() is None


def _task(task_id: str) -> TaskRun:
    sha = "c" * 40
    return TaskRun(
        schema_version=SCHEMA_VERSION,
        task_id=task_id,
        objective="runtime platform",
        created_at="2026-10-07T00:00:00+00:00",
        updated_at="2026-10-07T00:00:00+00:00",
        repository="/r/.git",
        worktree="/r",
        branch="b",
        baseline_sha=sha,
        current_sha=sha,
        phase=TaskPhase.IMPLEMENTING,
        status=CompletionState.ACTIVE,
        scope=TaskScope(),
    )


def test_agent_ledgers_are_served_read_only(
    api: Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = api
    TaskStore(tmp_path).save(_task("TB-RUN-20261007-0001"))
    (tmp_path / "tasks" / "TB-RUN-20261007-0002.json").write_text("{bad", encoding="utf-8")
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path))
    body = client.get("/runtime/agent", headers=READ).json()
    (task,) = body["tasks"]
    assert (task["phase"], task["status"], task["steps_total"]) == ("IMPLEMENTING", "ACTIVE", 0)
    assert body["branch_audit"] == []
    assert body["unreadable"][0].startswith("TB-RUN-20261007-0002")
    (tmp_path / "branch-audit.json").write_text("{bad", encoding="utf-8")
    assert agent_ledgers(tmp_path)["unreadable"][-1].startswith("branch-audit")
    monkeypatch.delenv(STATE_DIR_ENV)
    assert runtime_control._agent_state_dir().parts[-2:] == (".git", "agent-control")
