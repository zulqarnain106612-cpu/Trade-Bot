"""
TERM-001, TERM-003, TERM-004, TERM-006, SEC-0010 -- the daemon as its clients see it.

Two clients on the real Unix socket stand in for the dashboard and the CLI:
a session one of them creates is announced to the other, output reaches only
the clients attached to it, and a client that attaches late replays exactly
what it missed. Every refusal is exercised -- no hello, a wrong protocol, a
foreign peer, too many clients, an oversized frame, job registration from a
browser -- because a refusal nobody has seen happen is not known to happen.

The WebSocket listener is exercised through its handshake check and its
request loop with a stand-in connection: the suite's network guard refuses
TCP connects, and the loop is the same one the Unix socket runs.

Decides:
  - TERM-001 -- GUI and CLI clients share one set of sessions
  - TERM-003 -- terminal requests are validated at the boundary
  - TERM-004 -- the terminal is local-only and authenticated
  - TERM-006 -- closing a session or stopping the service leaves no orphan
  - SEC-0010 -- no path hands a shell to anyone but the local user who owns it
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import os

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedOK
from websockets.http11 import Request

from src.terminal import PROTOCOL_VERSION, SERVICE_VERSION, auth
from src.terminal.server import ClientConnection, DaemonError, TerminalServer, run_daemon

from ._support import FAST_LIMITS, AsyncClient, Daemon, alive, make_config

SHARED = "echo shared-$((2+2))"


def _output(frames, sid) -> bytes:
    chunks = [f["data"] for f in frames if f["t"] == "output" and f["sid"] == sid]
    return b"".join(base64.b64decode(chunk) for chunk in chunks)


async def _collect_output(client: AsyncClient, sid: str, needle: bytes) -> list[dict]:
    seen: list[dict] = []
    while needle not in _output(seen, sid):
        frame = await client.wait_for(lambda f: f["t"] == "output" and f["sid"] == sid)
        seen.append(frame)
    return seen


def finished(process_id: str):
    return lambda f: f["t"] == "process_done" and f["process"]["id"] == process_id


def session_event(sid: str):
    return lambda f: f["t"] == "session" and f["session"]["id"] == sid


def foreground_sleep(command: str | None = None, *, not_id: str | None = None):
    """A process event for a `sleep 30` the poller has identified by group."""

    def match(frame: dict) -> bool:
        process = frame.get("process") or {}
        return (
            frame["t"] == "process"
            and (command is None or process.get("command") == command)
            and process.get("id") != not_id
            and process.get("title") == "sleep 30"
            and bool(process.get("pgid"))
        )

    return match


async def _error_code(client: AsyncClient, kind: str, **fields) -> str:
    reply = await client.request(kind, **fields)
    return reply.get("code", "")


class TestHello:
    async def test_hello_returns_a_welcome_and_a_snapshot(self, daemon):
        client = await AsyncClient.connect(daemon.config.socket_path)
        try:
            assert client.snapshot["sessions"] == []
            assert client.snapshot["processes"] == {"active": [], "history": []}
            pong = await client.request("ping")
            assert pong["t"] == "ok" and pong["pong"] > 0
        finally:
            await client.close()

    async def test_the_welcome_describes_the_daemon(self, daemon):
        client = await AsyncClient.connect(daemon.config.socket_path, hello=False)
        try:
            welcome = await client.request("hello", protocol=PROTOCOL_VERSION, client="gui")
            assert welcome["version"] == SERVICE_VERSION
            assert welcome["server_pid"] == os.getpid()
            assert welcome["limits"]["max_sessions"] == FAST_LIMITS.max_sessions
            assert welcome["default_cwd"] == str(daemon.config.default_cwd)
        finally:
            await client.close()

    @pytest.mark.parametrize(
        ("first", "code"),
        [
            ({"t": "hello", "id": 1, "protocol": 99}, "protocol_mismatch"),
            ({"t": "session.create", "id": 1}, "unauthorized"),
        ],
    )
    async def test_a_bad_first_frame_closes_the_connection(self, daemon, first, code):
        client = await AsyncClient.connect(daemon.config.socket_path, hello=False)
        await client.send(first)
        error = await client.wait_for(lambda f: f["t"] == "error")
        assert error["code"] == code
        assert await client.closed()

    async def test_a_bad_client_kind_is_refused_but_hello_can_be_retried(self, daemon):
        client = await AsyncClient.connect(daemon.config.socket_path, hello=False)
        try:
            refused = await client.request("hello", protocol=PROTOCOL_VERSION, client="browser")
            assert refused["code"] == "bad_field"
            welcome = await client.request("hello", protocol=PROTOCOL_VERSION, client="cli")
            assert welcome["t"] == "welcome"
            again = await client.request("hello", protocol=PROTOCOL_VERSION)
            assert again["code"] == "unknown_type"
        finally:
            await client.close()

    async def test_malformed_frames_are_answered_not_fatal(self, daemon):
        client = await AsyncClient.connect(daemon.config.socket_path)
        try:
            await client.send(b"{not json")
            assert (await client.wait_for(lambda f: f["t"] == "error"))["code"] == "bad_json"
            assert await _error_code(client, "teleport") == "unknown_type"
            code = await _error_code(client, "session.input", sid="nope", data="x")
            assert code == "bad_session_id"
            code = await _error_code(client, "session.resize", sid="s-00000000", rows=1, cols=1)
            assert code == "not_found"
            code = await _error_code(client, "process.output", process_id="p-00000000")
            assert code == "not_found"
            assert (await client.request("ping"))["t"] == "ok"
        finally:
            await client.close()

    async def test_an_oversized_frame_ends_the_connection(self, daemon):
        client = await AsyncClient.connect(daemon.config.socket_path)
        padding = b"x" * (FAST_LIMITS.max_frame_bytes + 10)
        await client.send(b'{"t":"ping","pad":"' + padding + b'"}')
        error = await client.wait_for(lambda f: f["t"] == "error")
        assert error["code"] == "frame_too_large"
        assert await client.closed()


class TestSharedSessions:
    async def test_two_clients_see_one_set_of_sessions(self, daemon, short_tmp):
        gui = await AsyncClient.connect(daemon.config.socket_path, client="gui")
        cli = await AsyncClient.connect(daemon.config.socket_path, client="cli")
        try:
            created = await gui.request(
                "session.create",
                name="from-gui",
                cwd=str(short_tmp),
                rows=30,
                cols=90,
                attach=True,
            )
            session = created["session"]
            sid = session["id"]
            assert (session["name"], session["rows"], session["cols"]) == ("from-gui", 30, 90)
            # The other client is told about it without asking.
            announced = await cli.wait_for(session_event(sid))
            assert announced["session"]["name"] == "from-gui"

            await gui.request("session.input", sid=sid, data=SHARED + "\r")
            await _collect_output(gui, sid, b"shared-4")
            done = await cli.wait_for(
                lambda f: f["t"] == "process_done" and f["process"]["command"] == SHARED
            )
            assert done["process"]["state"] == "succeeded"
            # Not attached, so the other client received no output for it.
            await cli.request("ping")
            assert not [f for f in cli.frames if f["t"] == "output"]

            # Attaching late replays what was missed, from the start.
            attached = await cli.request("session.attach", sid=sid, since=0)
            assert attached["offset"] == 0 and attached["truncated"] is False
            replay = await _collect_output(cli, sid, b"shared-4")
            assert all(f.get("replay") for f in replay)

            detail = await cli.request("process.output", process_id=done["process"]["id"])
            assert b"shared-4" in base64.b64decode(detail["process"]["output"])

            resized = await cli.request("session.resize", sid=sid, rows=40, cols=120)
            assert resized["t"] == "ok"
            assert (await cli.request("session.rename", sid=sid, name="renamed"))["t"] == "ok"
            renamed = await gui.wait_for(
                lambda f: f["t"] == "session" and f["session"]["name"] == "renamed"
            )
            assert renamed["session"]["rows"] == 40
            assert (await cli.request("session.detach", sid=sid))["sid"] == sid

            closing = await cli.request("session.close", sid=sid)
            assert closing["closing"] is True
            await gui.wait_for(lambda f: f["t"] == "session_removed" and f["id"] == sid)
        finally:
            await gui.close()
            await cli.close()

    async def test_running_output_signals_and_kill(self, daemon):
        client = await AsyncClient.connect(daemon.config.socket_path)
        try:
            sid = (await client.request("session.create"))["session"]["id"]
            command = "head -c 6000 /dev/zero | tr '\\0' y; sleep 30"
            await client.request("session.input", sid=sid, data=command + "\r")
            running = await client.wait_for(foreground_sleep(command))
            entry = running["process"]
            reply = await client.request("process.output", process_id=entry["id"])
            detail = reply["process"]
            assert detail["output_truncated"] is True
            retained = base64.b64decode(detail["output"])
            assert len(retained) == FAST_LIMITS.history_output_bytes

            killed = await client.request("process.kill", process_id=entry["id"], signal="TERM")
            assert killed["pgid"] == entry["pgid"]
            done = await client.wait_for(finished(entry["id"]))
            assert done["process"]["exit_code"] == 143

            client.frames.clear()
            await client.request("session.input", sid=sid, data="sleep 30\r")
            await client.wait_for(foreground_sleep(not_id=entry["id"]))
            interrupted = await client.request("session.signal", sid=sid, signal="INT")
            assert interrupted["pgid"] > 0
            code = await _error_code(client, "session.signal", sid=sid, signal="STOP")
            assert code == "bad_signal"
        finally:
            await client.close()

    async def test_a_job_and_its_result(self, daemon):
        client = await AsyncClient.connect(daemon.config.socket_path)
        try:
            argv = ["sh", "-c", "echo built; exit 2"]
            created = await client.request("session.create", argv=argv, name="build")
            job_id = created["session"]["job_entry"]
            done = await client.wait_for(finished(job_id))
            assert (done["process"]["state"], done["process"]["exit_code"]) == ("failed", 2)
            assert await _error_code(client, "session.create", argv="sh -c ls") == "bad_argv"
            assert await _error_code(client, "session.create", cwd="relative") == "bad_cwd"
        finally:
            await client.close()


class TestJobRegistration:
    async def test_an_application_job_runs_through_the_registry(self, daemon):
        client = await AsyncClient.connect(daemon.config.socket_path, client="api")
        try:
            begun = await client.request("job.begin", name="backfill 1h", detail="days=30")
            job_id = begun["process_id"]
            await client.request("job.output", process_id=job_id, text="bars_written=10\n")
            await client.request("job.end", process_id=job_id, ok=True)
            done = await client.wait_for(finished(job_id))
            assert (done["process"]["kind"], done["process"]["state"]) == ("app", "succeeded")
            reply = await client.request("process.output", process_id=job_id)
            assert base64.b64decode(reply["process"]["output"]) == b"bars_written=10\n"

            begun = await client.request("job.begin", name="retrain", process_id="p-0000beef")
            failed = begun["process_id"]
            assert failed == "p-0000beef"
            code = await _error_code(client, "job.begin", name="x", process_id=failed)
            assert code == "duplicate_id"
            code = await _error_code(client, "job.end", process_id=failed, ok="no")
            assert code == "bad_field"
            error = "ValueError: no data"
            await client.request("job.end", process_id=failed, ok=False, error=error)
            gone = await client.wait_for(finished(failed))
            assert gone["process"]["failure_reason"] == "ValueError: no data"
            assert gone["process"]["state"] == "failed"
            code = await _error_code(client, "process.kill", process_id=failed)
            assert code == "not_running"
        finally:
            await client.close()

    async def test_job_requests_cannot_touch_terminal_processes(self, daemon):
        client = await AsyncClient.connect(daemon.config.socket_path)
        try:
            job = await client.request("session.create", argv=["sleep", "30"])
            entry = job["session"]["job_entry"]
            refused = await client.request("job.end", process_id=entry, ok=True)
            assert refused["code"] == "forbidden"
        finally:
            await client.close()


class TestConnectionLimits:
    async def test_beyond_the_client_limit_a_connection_is_refused(self, daemon):
        path = daemon.config.socket_path
        clients = [await AsyncClient.connect(path) for _ in range(FAST_LIMITS.max_clients)]
        try:
            extra = await AsyncClient.connect(path, hello=False)
            error = await extra.wait_for(lambda f: f["t"] == "error")
            assert error["code"] == "too_many_clients"
            assert await extra.closed()
        finally:
            for client in clients:
                await client.close()

    async def test_a_silent_connection_is_dropped(self, short_tmp):
        limits = dataclasses.replace(FAST_LIMITS, hello_timeout_s=0.05)
        config = make_config(short_tmp, limits=limits)
        d = await Daemon(config, short_tmp).started()
        try:
            client = await AsyncClient.connect(config.socket_path, hello=False)
            error = await client.wait_for(lambda f: f["t"] == "error")
            assert error["code"] == "hello_timeout"
            assert await client.closed()
        finally:
            await d.shutdown()

    async def test_a_peer_running_as_another_user_is_refused(self, daemon, monkeypatch):
        monkeypatch.setattr(auth, "peer_uid", lambda _sock: os.getuid() + 1)
        client = await AsyncClient.connect(daemon.config.socket_path, hello=False)
        assert await client.closed()


class TestLifecycle:
    async def test_stopping_ends_every_session_and_removes_files(self, config, short_tmp):
        d = await Daemon(config, short_tmp, install_signals=True).started()
        client = await AsyncClient.connect(config.socket_path)
        job = await client.request("session.create", argv=["sleep", "30"])
        pid = job["session"]["pid"]
        assert config.pid_path.read_text(encoding="utf-8").strip() == str(os.getpid())
        assert (config.socket_path.stat().st_mode & 0o777) == 0o600
        await d.shutdown()
        assert not alive(pid)
        assert not config.socket_path.exists()
        assert not config.pid_path.exists()
        assert await client.closed()

    async def test_a_second_daemon_is_refused(self, daemon):
        with pytest.raises(DaemonError, match="already running"):
            await run_daemon(daemon.config, install_signals=False, stop=asyncio.Event())

    async def test_root_is_refused_unless_allowed(self, config, monkeypatch):
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        with pytest.raises(DaemonError, match="refusing to run as root"):
            await run_daemon(config, install_signals=False, stop=asyncio.Event())

    async def test_the_websocket_listener_binds_loopback_only(self, short_tmp):
        config = make_config(short_tmp, ws_port=0)
        d = await Daemon(config, short_tmp).started()
        try:
            assert d.server.ws_port and d.server.ws_port > 0
            (sockname,) = [s.getsockname() for s in d.server._ws_server.sockets]
            assert sockname[0] == "127.0.0.1"
        finally:
            await d.shutdown()

    async def test_a_failed_background_task_is_logged_not_raised(self, config):
        server = TerminalServer(config)

        async def boom() -> None:
            raise RuntimeError("background failure")

        task = server.spawn(boom(), name="boom")
        await asyncio.gather(task, return_exceptions=True)
        assert task not in server._tasks


class FakeWebSocket:
    """Stands in for a websockets ServerConnection in the shared request loop."""

    def __init__(self, *messages, closed_by_peer: bool = False) -> None:
        self._messages = list(messages)
        self._closed_by_peer = closed_by_peer
        self.sent: list[dict] = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        if self._closed_by_peer:
            raise ConnectionClosedOK(None, None)
        raise StopAsyncIteration

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def close(self) -> None:
        self.closed = True


class FakeHandshake:
    def respond(self, status, text):
        return ("refused", int(status), text)


@pytest.fixture
def ws_server(config):
    auth.load_or_create_token(config.token_path)
    server = TerminalServer(config)
    server.ws_port = 8766
    return server


def _request(*headers):
    return Request("/", Headers(list(headers)))


def _hello(token: str | None) -> str:
    frame = {"t": "hello", "id": 1, "protocol": PROTOCOL_VERSION}
    if token is not None:
        frame["token"] = token
    return json.dumps(frame)


LOCAL = ("Host", "127.0.0.1:8766")
DASHBOARD = ("Origin", "http://localhost:5173")


class TestWebSocket:
    @pytest.mark.parametrize(
        "headers",
        [
            [LOCAL, DASHBOARD],
            [("Host", "localhost:8766"), ("Origin", "http://127.0.0.1:5173")],
            [("Host", "[::1]:8766")],
        ],
    )
    def test_the_dashboard_origin_on_loopback_is_admitted(self, ws_server, headers):
        assert ws_server._check_handshake(FakeHandshake(), _request(*headers)) is None

    @pytest.mark.parametrize(
        "headers",
        [
            [("Host", "evil.example:8766"), DASHBOARD],
            [("Host", "127.0.0.1:9999"), DASHBOARD],
            [LOCAL, ("Host", "evil.example:8766")],
            [DASHBOARD],
            [LOCAL, ("Origin", "https://evil.example")],
            [LOCAL, ("Origin", "null")],
            [LOCAL, DASHBOARD, ("Origin", "https://evil.example")],
        ],
    )
    def test_anything_else_is_refused_before_the_socket_opens(self, ws_server, headers):
        assert ws_server._check_handshake(FakeHandshake(), _request(*headers))[1] == 403

    async def test_a_browser_needs_the_token(self, ws_server, config):
        token = auth.load_or_create_token(config.token_path)
        good = FakeWebSocket(
            _hello(token),
            json.dumps({"t": "ping", "id": 2}),
            json.dumps({"t": "job.begin", "id": 3, "name": "x"}),
            closed_by_peer=True,
        )
        await ws_server._handle_ws(good)
        kinds = [(f["t"], f.get("code")) for f in good.sent]
        assert kinds[:3] == [("welcome", None), ("snapshot", None), ("ok", None)]
        assert ("error", "forbidden") in kinds
        assert good.closed

        bad = FakeWebSocket(_hello("0" * 64))
        await ws_server._handle_ws(bad)
        assert [f.get("code") for f in bad.sent] == ["unauthorized"]

        missing = FakeWebSocket(_hello(None))
        await ws_server._handle_ws(missing)
        assert [f.get("code") for f in missing.sent] == ["unauthorized"]


async def _never():
    await asyncio.Event().wait()
    yield b""  # pragma: no cover - never reached


class TestClientConnection:
    async def test_a_client_that_cannot_keep_up_is_cut_off(self, config):
        sent: list[str] = []

        async def send(message: str) -> None:
            sent.append(message)

        async def close() -> None:
            return None

        connection = ClientConnection(transport="unix", send=send, close=close, queue_limit=2)
        connection.push({"t": "a"})
        connection.push("raw")
        connection.push({"t": "overflow"})
        assert connection.overflowed.is_set()
        connection.push({"t": "ignored"})

        server = TerminalServer(config)
        await asyncio.wait_for(server._serve(connection, _never()), 5)
        assert sent == ['{"t":"a"}', "raw"]

    async def test_a_dead_transport_ends_the_loop(self, config):
        async def send(message: str) -> None:
            raise ConnectionResetError

        async def close() -> None:
            raise OSError("already closed")

        connection = ClientConnection(transport="unix", send=send, close=close, queue_limit=4)
        connection.push({"t": "x"})
        server = TerminalServer(config)
        await asyncio.wait_for(server._serve(connection, _never()), 5)
        assert connection not in server._clients
