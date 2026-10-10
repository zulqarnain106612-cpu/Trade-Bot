"""
TERM-007 -- the API's long operations appear in the process center, and
reporting them can never change their outcome.

A backfill and a manual retrain are registered as application jobs with the
terminal service. The reporter is replaced by a recorder here: what matters is
that the endpoints report the real outcome (rows written, the exception that
ended a retrain, a cancellation) and that the endpoint's own response and
status are exactly what they were without reporting.

Decides:
  - TERM-007 -- the host CLI and application jobs share the process registry
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.config import Timeframe

_KEY = "test-key-" + "a" * 32  # pragma: allowlist secret
_SECRET = "operator-secret-" + "b" * 32  # pragma: allowlist secret


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def begin(self, name: str, detail: str | None = None) -> str:
        self.events.append(("begin", name, detail))
        return "p-0000abcd"

    def output(self, job_id: str, text: str) -> None:
        self.events.append(("output", job_id, text))

    def end(self, job_id: str, *, ok: bool, error: str | None = None) -> None:
        self.events.append(("end", job_id, ok, error))


class FakeTask:
    def __init__(self) -> None:
        self.callbacks: list = []

    def add_done_callback(self, callback) -> None:
        self.callbacks.append(callback)


class Finished:
    def __init__(self, *, cancelled: bool = False, error: BaseException | None = None) -> None:
        self._cancelled = cancelled
        self._error = error

    def cancelled(self) -> bool:
        return self._cancelled

    def exception(self) -> BaseException | None:
        return self._error


class FakeOrchestrator:
    def __init__(self) -> None:
        self.backfill_error: Exception | None = None
        self.task: FakeTask | None = FakeTask()

    async def request_backfill(self, tf: Timeframe, lookback_days: int) -> int:
        if self.backfill_error is not None:
            raise self.backfill_error
        return 42

    def request_retrain(self, tf: Timeframe) -> str:
        return "started"

    def retrain_task(self, tf: Timeframe) -> FakeTask | None:
        return self.task


class FakeStorage:
    async def insert_audit_event(self, **event: Any) -> None:
        return None


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", _KEY)
    monkeypatch.setenv("OPERATOR_SECRET", _SECRET)
    from fastapi.testclient import TestClient

    from src.api import main as api_main

    recorder = Recorder()
    orchestrator = FakeOrchestrator()
    api_main._state.ready = True
    monkeypatch.setattr(api_main, "_job_reporter", recorder)
    monkeypatch.setattr(api_main, "require_orchestrator", lambda: orchestrator)
    monkeypatch.setattr(api_main._state, "storage", FakeStorage(), raising=False)
    monkeypatch.setattr(api_main._state, "_endpoint_hits", {})
    client = TestClient(api_main.app, raise_server_exceptions=False)
    client.headers.update({"x-api-key": _KEY})
    client.recorder, client.orchestrator = recorder, orchestrator  # type: ignore[attr-defined]
    return client


class TestBackfill:
    def test_a_backfill_is_reported_with_its_result(self, api):
        response = api.post("/backfill", json={"timeframe": "15m", "lookback_days": 30})
        assert response.status_code == 200 and response.json()["bars_written"] == 42
        assert api.recorder.events == [
            ("begin", "backfill 15m", "lookback_days=30"),
            ("output", "p-0000abcd", "bars_written=42\n"),
            ("end", "p-0000abcd", True, None),
        ]

    def test_a_failed_backfill_is_reported_failed_and_still_fails(self, api):
        api.orchestrator.backfill_error = RuntimeError("exchange said no")
        response = api.post("/backfill", json={"timeframe": "15m", "lookback_days": 30})
        assert response.status_code == 500
        expected = ("end", "p-0000abcd", False, "RuntimeError: exchange said no")
        assert api.recorder.events[-1] == expected


class TestRetrain:
    def _start(self, api):
        body = {"timeframe": "4h", "operator": "alice", "operator_secret": _SECRET}
        response = api.post("/models/retrain", json=body)
        assert response.status_code == 200 and response.json()["status"] == "started"

    @pytest.mark.parametrize(
        ("finished", "expected"),
        [
            (Finished(), ("end", "p-0000abcd", True, None)),
            (Finished(cancelled=True), ("end", "p-0000abcd", False, "cancelled")),
            (
                Finished(error=ValueError("no data")),
                ("end", "p-0000abcd", False, "ValueError: no data"),
            ),
        ],
    )
    def test_the_retrain_is_followed_to_its_real_outcome(self, api, finished, expected):
        self._start(api)
        assert api.recorder.events == [("begin", "model retrain 4h", "started by alice")]
        (callback,) = api.orchestrator.task.callbacks
        callback(finished)
        assert api.recorder.events[-1] == expected

    def test_without_a_task_nothing_is_reported(self, api):
        api.orchestrator.task = None
        self._start(api)
        assert api.recorder.events == []


class TestReporterConstruction:
    def test_the_reporter_points_at_the_terminal_socket(self, monkeypatch, tmp_path):
        from src.api import main as api_main

        monkeypatch.setenv("TB_TERMINAL_RUNTIME_DIR", str(tmp_path))
        reporter = api_main._terminal_job_reporter()
        assert reporter._socket_path == tmp_path / "termd.sock"

    def test_a_bad_terminal_configuration_disables_reporting_not_the_api(self, monkeypatch):
        from src.api import main as api_main

        monkeypatch.setenv("TB_TERMINAL_WS_HOST", "0.0.0.0")
        reporter = api_main._terminal_job_reporter()
        assert reporter._socket_path is None

    def test_job_errors_are_redacted_and_bounded(self):
        from src.api import main as api_main

        text = api_main._job_error(RuntimeError("password=hunter2hunter2 " + "y" * 3000))
        assert "hunter2hunter2" not in text and len(text) <= 1024


class TestOrchestratorAccessor:
    def _orchestrator(self):
        from unittest.mock import MagicMock

        from src.engine.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._retrain_tasks = {}
        orch._log = MagicMock()
        return orch

    async def test_only_an_unfinished_retrain_is_returned(self):
        orch = self._orchestrator()
        assert orch.retrain_task(Timeframe.SWING) is None
        gate = asyncio.Event()
        task = asyncio.ensure_future(gate.wait())
        orch._retrain_tasks[Timeframe.SWING.value] = task
        assert orch.retrain_task(Timeframe.SWING) is task
        gate.set()
        await task
        assert orch.retrain_task(Timeframe.SWING) is None
