"""
The terminal service: one daemon, two listeners, one set of sessions.

``TerminalServer`` accepts clients on a Unix socket (CLI, Electron main
process, trading API) and, optionally, on a loopback WebSocket (a dashboard
in an ordinary browser). Both kinds of connection run the same request loop
against the same SessionManager and ProcessRegistry, so a session created by
one client is visible to every other client the moment it exists.

State changes are pushed. A client that says hello receives a full snapshot
and then every change as it happens; a client that reconnects simply says
hello again and the snapshot replaces whatever it held, so nothing stale
survives a reconnect. Output reaches only the connections attached to that
session. Each connection has a bounded send queue: a client that cannot keep
up is disconnected (it reconnects and resyncs) rather than allowed to stall a
PTY or the daemon.

Registry: TERM-001, TERM-003, TERM-004, SEC-0010 (config/quality_registry.json).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any

import structlog
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from src.terminal import PROTOCOL_VERSION, SERVICE_VERSION, auth, protocol
from src.terminal.config import TerminalConfig, ensure_private_dir
from src.terminal.protocol import ProtocolError, b64encode, encode_frame
from src.terminal.registry import ProcessRegistry
from src.terminal.sessions import SessionManager
from src.terminal.shell_integration import write_rcfile

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

REPLAY_CHUNK = 64 * 1024
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})


class DaemonError(RuntimeError):
    """The daemon refuses to start (already running, running as root, ...)."""


class ClientConnection:
    """One connected client and everything the server tracks about it."""

    def __init__(
        self,
        *,
        transport: str,
        send: Callable[[str], Awaitable[None]],
        close: Callable[[], Awaitable[None]],
        queue_limit: int,
    ) -> None:
        self.transport = transport
        self.client_kind: str | None = None
        self.authenticated = False
        self.attached: set[str] = set()
        self._send = send
        self._close = close
        self._queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=queue_limit)
        self.overflowed = asyncio.Event()

    def push(self, frame: dict[str, Any] | str) -> None:
        if self.overflowed.is_set():
            return
        message = frame if isinstance(frame, str) else encode_frame(frame)
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            log.warning("terminal.client_overflow", transport=self.transport)
            self.overflowed.set()

    async def run_writer(self) -> None:
        while True:
            message = await self._queue.get()
            if message is None:
                return
            await self._send(message)

    async def drain_and_stop(self) -> None:
        """Let queued frames (an error, say) go out before the writer stops."""
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)

    async def close(self) -> None:
        with contextlib.suppress(OSError, ConnectionClosed):
            await self._close()


class TerminalServer:
    def __init__(
        self,
        config: TerminalConfig,
        *,
        base_env: dict[str, str] | None = None,
        shell: str | None = None,
    ) -> None:
        self.config = config
        self.started_at = time.time()
        self.registry = ProcessRegistry(
            history_limit=config.limits.history_entries,
            output_limit=config.limits.history_output_bytes,
            on_event=self._on_process_event,
        )
        self.manager = SessionManager(
            config,
            self.registry,
            on_session=self._on_session_event,
            on_output=self._on_output,
            base_env=base_env,
            shell=shell,
        )
        self._clients: set[ClientConnection] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._unix_server: asyncio.AbstractServer | None = None
        self._ws_server: Server | None = None
        self.ws_port: int | None = None

    # -- listeners ---------------------------------------------------------

    async def start(self) -> None:
        path = self.config.socket_path
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self._unix_server = await asyncio.start_unix_server(
            self._handle_unix, path=str(path), limit=self.config.limits.max_frame_bytes
        )
        os.chmod(path, 0o600)
        if self.config.ws_port is not None:
            self._ws_server = await serve(
                self._handle_ws,
                self.config.ws_host,
                self.config.ws_port,
                process_request=self._check_handshake,
                max_size=self.config.limits.max_frame_bytes,
            )
            self.ws_port = self._ws_server.sockets[0].getsockname()[1]
        log.info(
            "terminal.listening",
            socket=str(path),
            ws_host=self.config.ws_host if self._ws_server else None,
            ws_port=self.ws_port,
        )

    async def stop(self) -> None:
        # Stop accepting, then close the clients, then wait: from Python 3.12
        # wait_closed() also waits for open connections, so waiting before the
        # clients are closed would never return.
        if self._ws_server is not None:
            self._ws_server.close()
        if self._unix_server is not None:
            self._unix_server.close()
        await self.manager.shutdown()
        for client in list(self._clients):
            await client.close()
        if self._ws_server is not None:
            await self._ws_server.wait_closed()
        if self._unix_server is not None:
            await self._unix_server.wait_closed()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(FileNotFoundError):
            self.config.socket_path.unlink()

    def spawn(self, coroutine: Awaitable[Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("terminal.task_failed", task=task.get_name(), error=str(task.exception()))

    # -- WebSocket ----------------------------------------------------------

    def _check_handshake(self, connection: ServerConnection, request: Request) -> Response | None:
        """
        Refuse a browser upgrade from anywhere but the dashboard.

        Host must be a loopback name with this port: a DNS-rebinding page
        reaches 127.0.0.1 under its own hostname, and this is where that
        shows. A browser always sends Origin; a page on any site other than
        the configured dashboard origins is refused before the socket opens.
        A missing Origin is a non-browser client, which still has to present
        the token in its first frame.
        """
        # get_all, not get: a repeated header makes get() raise, and "which of
        # two Hosts is real" is not a question worth answering -- refuse.
        hosts = request.headers.get_all("Host")
        host = hosts[0].strip().lower() if len(hosts) == 1 else ""
        if host not in {f"{name}:{self.ws_port}" for name in _LOOPBACK_HOSTS}:
            log.warning("terminal.ws_refused_host")
            return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden host\n")
        origins = request.headers.get_all("Origin")
        if len(origins) > 1 or (origins and origins[0] not in self.config.allowed_origins):
            log.warning("terminal.ws_refused_origin")
            return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden origin\n")
        return None

    async def _handle_ws(self, websocket: ServerConnection) -> None:
        async def incoming() -> AsyncIterator[str | bytes]:
            with contextlib.suppress(ConnectionClosed):
                async for message in websocket:
                    yield message

        client = ClientConnection(
            transport="ws",
            send=websocket.send,
            close=websocket.close,
            queue_limit=self.config.limits.send_queue_frames,
        )
        await self._serve(client, incoming())

    # -- Unix socket ---------------------------------------------------------

    async def _handle_unix(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        uid = auth.peer_uid(sock) if sock is not None else None
        if uid is not None and uid != os.getuid():
            log.warning("terminal.unix_refused_peer", peer_uid=uid)
            writer.close()
            return

        async def send(message: str) -> None:
            writer.write(message.encode("utf-8") + b"\n")
            await writer.drain()

        async def close() -> None:
            writer.close()
            await writer.wait_closed()

        async def incoming() -> AsyncIterator[str | bytes]:
            while True:
                try:
                    line = await reader.readline()
                except ValueError:
                    # StreamReader's way of saying the line exceeded `limit`.
                    yield b"x" * (self.config.limits.max_frame_bytes + 1)
                    return
                except ConnectionError:
                    return
                if not line:
                    return
                yield line

        client = ClientConnection(
            transport="unix",
            send=send,
            close=close,
            queue_limit=self.config.limits.send_queue_frames,
        )
        await self._serve(client, incoming())

    # -- the request loop ------------------------------------------------------

    async def _serve(self, client: ClientConnection, incoming: AsyncIterator[str | bytes]) -> None:
        if len(self._clients) >= self.config.limits.max_clients:
            client.push(_error(None, ProtocolError("too_many_clients", "client limit reached")))
            await client.drain_and_stop()
            await client.run_writer()
            await client.close()
            return
        self._clients.add(client)
        writer = asyncio.ensure_future(client.run_writer())
        overflow = asyncio.ensure_future(client.overflowed.wait())
        try:
            first = True
            while True:
                timeout = self.config.limits.hello_timeout_s if first else None
                reader = asyncio.ensure_future(_next(incoming))
                done, _ = await asyncio.wait(
                    {reader, overflow, writer},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if reader not in done:
                    reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                        await reader
                    if not done:
                        client.push(_error(None, ProtocolError("hello_timeout", "no hello")))
                    break
                raw = reader.result()
                if raw is None:
                    break
                first = False
                keep_going = await self._handle_raw(client, raw)
                if not keep_going:
                    break
        finally:
            self._clients.discard(client)
            client.attached.clear()
            overflow.cancel()
            await client.drain_and_stop()
            with contextlib.suppress(TimeoutError, OSError, ConnectionClosed):
                await asyncio.wait_for(writer, timeout=1.0)
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError, ConnectionClosed):
                await writer
            await client.close()

    async def _handle_raw(self, client: ClientConnection, raw: str | bytes) -> bool:
        try:
            frame = protocol.decode_frame(raw, self.config.limits.max_frame_bytes)
        except ProtocolError as exc:
            client.push(_error(None, exc))
            return exc.code != "frame_too_large"
        request_id = frame.get("id")
        try:
            frames = await self._dispatch(client, frame)
        except ProtocolError as exc:
            client.push(_error(request_id, exc))
            return client.authenticated or exc.code not in ("unauthorized", "protocol_mismatch")
        for item in frames:
            client.push(item)
        return True

    async def _dispatch(self, client: ClientConnection, frame: dict[str, Any]) -> list[dict[str, Any]]:
        kind = frame["t"]
        request_id = frame.get("id")
        if not client.authenticated:
            if kind != "hello":
                raise ProtocolError("unauthorized", "say hello first")
            return self._hello(client, frame)
        if kind in protocol.JOB_REQUESTS:
            if client.transport != "unix":
                raise ProtocolError("forbidden", f"{kind} is only accepted on the local socket")
            return _reply(request_id, self._job(kind, frame))
        if kind not in protocol.CLIENT_REQUESTS or kind == "hello":
            raise ProtocolError("unknown_type", f"unsupported request {kind!r}")
        handler = getattr(self, "_req_" + kind.replace(".", "_"))
        result = handler(client, frame)
        if asyncio.iscoroutine(result):
            result = await result
        payload, after = result if isinstance(result, tuple) else (result, [])
        return [*_reply(request_id, payload), *after]

    def _hello(self, client: ClientConnection, frame: dict[str, Any]) -> list[dict[str, Any]]:
        if frame.get("protocol") != PROTOCOL_VERSION:
            raise ProtocolError(
                "protocol_mismatch", f"this daemon speaks protocol {PROTOCOL_VERSION}"
            )
        kind = frame.get("client", "gui")
        if kind not in protocol.CLIENT_KINDS:
            raise ProtocolError("bad_field", f"client must be one of {sorted(protocol.CLIENT_KINDS)}")
        if client.transport == "ws" and not auth.token_accepted(
            self.config.token_path, frame.get("token")
        ):
            log.warning("terminal.ws_bad_token")
            raise ProtocolError("unauthorized", "the terminal token was not accepted")
        client.authenticated = True
        client.client_kind = kind
        welcome = {
            "t": "welcome",
            "id": frame.get("id"),
            "protocol": PROTOCOL_VERSION,
            "version": SERVICE_VERSION,
            "server_pid": os.getpid(),
            "started_at": self.started_at,
            "default_cwd": str(self.config.default_cwd),
            "limits": {
                "max_sessions": self.config.limits.max_sessions,
                "max_input_chars": self.config.limits.max_input_chars,
                "buffer_bytes": self.config.limits.buffer_bytes,
            },
        }
        snapshot = {
            "t": "snapshot",
            "sessions": self.manager.snapshot(),
            "processes": self.registry.snapshot(),
        }
        return [welcome, snapshot]

    # -- requests -------------------------------------------------------------

    def _req_ping(self, client: ClientConnection, frame: dict[str, Any]) -> dict[str, Any]:
        return {"pong": time.time()}

    async def _req_session_create(
        self, client: ClientConnection, frame: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        argv = frame.get("argv")
        session = await self.manager.create(
            name=protocol.optional_name(frame),
            cwd=protocol.validate_cwd(frame.get("cwd"), self.config.default_cwd),
            rows=protocol.bounded_int(frame, "rows", 1, protocol.MAX_ROWS, 24),
            cols=protocol.bounded_int(frame, "cols", 1, protocol.MAX_COLS, 80),
            argv=None if argv is None else protocol.validate_argv(argv),
        )
        after: list[dict[str, Any]] = []
        if frame.get("attach") is True:
            client.attached.add(session.id)
            after = self._replay(session.id, 0)
        return {"session": self.manager.describe(session.id)}, after

    def _req_session_attach(
        self, client: ClientConnection, frame: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        sid = protocol.session_id(frame)
        since = protocol.optional_offset(frame)
        session = self.manager.get(sid)
        offset, _data, truncated = session.buffer.read(since)
        client.attached.add(sid)
        return (
            {"sid": sid, "offset": offset, "end": session.buffer.end, "truncated": truncated},
            self._replay(sid, offset),
        )

    def _replay(self, sid: str, since: int) -> list[dict[str, Any]]:
        session = self.manager.get(sid)
        offset, data, _truncated = session.buffer.read(since)
        frames = []
        for start in range(0, len(data), REPLAY_CHUNK):
            chunk = data[start : start + REPLAY_CHUNK]
            frames.append(
                {
                    "t": "output",
                    "sid": sid,
                    "offset": offset + start,
                    "data": b64encode(chunk),
                    "replay": True,
                }
            )
        return frames

    def _req_session_detach(self, client: ClientConnection, frame: dict[str, Any]) -> dict[str, Any]:
        sid = protocol.session_id(frame)
        client.attached.discard(sid)
        return {"sid": sid}

    def _req_session_input(self, client: ClientConnection, frame: dict[str, Any]) -> dict[str, Any]:
        sid = protocol.session_id(frame)
        data = protocol.text_field(frame, "data", self.config.limits.max_input_chars)
        self.manager.write(sid, data.encode("utf-8", errors="replace"))
        return {"sid": sid}

    def _req_session_resize(self, client: ClientConnection, frame: dict[str, Any]) -> dict[str, Any]:
        sid = protocol.session_id(frame)
        rows = protocol.bounded_int(frame, "rows", 1, protocol.MAX_ROWS)
        cols = protocol.bounded_int(frame, "cols", 1, protocol.MAX_COLS)
        self.manager.resize(sid, rows, cols)
        return {"sid": sid}

    def _req_session_rename(self, client: ClientConnection, frame: dict[str, Any]) -> dict[str, Any]:
        sid = protocol.session_id(frame)
        self.manager.rename(sid, protocol.validate_name(frame.get("name")))
        return {"sid": sid}

    def _req_session_signal(self, client: ClientConnection, frame: dict[str, Any]) -> dict[str, Any]:
        sid = protocol.session_id(frame)
        signum = protocol.signal_from(frame, "INT")
        group = self.manager.signal_foreground(sid, signum)
        return {"sid": sid, "pgid": group}

    def _req_session_close(self, client: ClientConnection, frame: dict[str, Any]) -> dict[str, Any]:
        sid = protocol.session_id(frame)
        self.manager.get(sid)
        # Closing waits for the session's processes to leave; the reply should
        # not, and the session_removed event says when it is done.
        self.spawn(self.manager.close(sid), name=f"close-{sid}")
        return {"sid": sid, "closing": True}

    def _req_process_kill(self, client: ClientConnection, frame: dict[str, Any]) -> dict[str, Any]:
        entry_id = protocol.process_id(frame)
        signum = protocol.signal_from(frame, "TERM")
        group = self.manager.kill_process(entry_id, signum)
        return {"process_id": entry_id, "pgid": group}

    def _req_process_output(self, client: ClientConnection, frame: dict[str, Any]) -> dict[str, Any]:
        entry_id = protocol.process_id(frame)
        entry = self.registry.get(entry_id)
        payload = entry.to_dict(include_output=True, now_mono=time.monotonic())
        if entry.state == "running" and entry.session_id is not None:
            session = self.manager.get(entry.session_id)
            start = entry.output_start if entry.output_start is not None else session.buffer.end
            data, truncated = session.buffer.slice(start, session.buffer.end)
            limit = self.config.limits.history_output_bytes
            if len(data) > limit:
                data, truncated = data[-limit:], True
            payload["output"] = b64encode(data)
            payload["output_truncated"] = truncated
        return {"process": payload}

    def _job(self, kind: str, frame: dict[str, Any]) -> dict[str, Any]:
        if kind == "job.begin":
            entry = self.registry.begin(
                kind="app",
                title=protocol.validate_name(frame.get("name")),
                entry_id=None if frame.get("process_id") is None else protocol.process_id(frame),
                detail=None
                if frame.get("detail") is None
                else protocol.text_field(frame, "detail", protocol.MAX_JOB_TEXT_CHARS),
            )
            return {"process_id": entry.id}
        entry_id = protocol.process_id(frame)
        entry = self.registry.get(entry_id)
        if entry.kind != "app":
            raise ProtocolError("forbidden", f"{entry_id} is not an application job")
        if kind == "job.output":
            text = protocol.text_field(frame, "text", protocol.MAX_JOB_TEXT_CHARS)
            self.registry.append_output(entry_id, text.encode("utf-8", errors="replace"))
            return {"process_id": entry_id}
        ok = frame.get("ok")
        if not isinstance(ok, bool):
            raise ProtocolError("bad_field", "ok must be true or false")
        error = frame.get("error")
        reason = None if error is None else protocol.text_field(frame, "error", 1024)
        self.registry.finish(entry_id, exit_code=None, reason=reason, reported_ok=ok)
        return {"process_id": entry_id}

    # -- event fan-out -----------------------------------------------------------

    def _broadcast(self, frame: dict[str, Any]) -> None:
        message = encode_frame(frame)
        for client in list(self._clients):
            if client.authenticated:
                client.push(message)

    def _on_session_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "session_removed":
            self._broadcast({"t": "session_removed", "id": payload["id"]})
        else:
            self._broadcast({"t": "session", "session": payload})

    def _on_process_event(self, kind: str, payload: dict[str, Any]) -> None:
        self._broadcast({"t": kind, "process": payload})

    def _on_output(self, sid: str, offset: int, data: bytes) -> None:
        message = encode_frame({"t": "output", "sid": sid, "offset": offset, "data": b64encode(data)})
        for client in list(self._clients):
            if sid in client.attached:
                client.push(message)


async def _next(incoming: AsyncIterator[str | bytes]) -> str | bytes | None:
    try:
        return await incoming.__anext__()
    except StopAsyncIteration:
        return None


def _reply(request_id: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if request_id is None:
        return []
    return [{"t": "ok", "id": request_id, **payload}]


def _error(request_id: Any, exc: ProtocolError) -> dict[str, Any]:
    return {"t": "error", "id": request_id, "code": exc.code, "message": exc.message}


# -- daemon lifecycle -----------------------------------------------------------


class InstanceLock:
    """An exclusive flock on the runtime lock file; held for the daemon's life."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise DaemonError("another terminal daemon is already running") from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


async def run_daemon(
    config: TerminalConfig,
    *,
    allow_root: bool = False,
    stop: asyncio.Event | None = None,
    install_signals: bool = True,
    ready: Callable[[TerminalServer], None] | None = None,
    base_env: dict[str, str] | None = None,
    shell: str | None = None,
) -> None:
    """Run the service until *stop* is set or SIGTERM/SIGINT arrives."""
    if os.geteuid() == 0 and not allow_root:
        raise DaemonError(
            "refusing to run as root: the terminal runs commands with the daemon's "
            "privileges; start it as the logged-in user (or pass --allow-root)"
        )
    ensure_private_dir(config.runtime_dir)
    ensure_private_dir(config.state_dir)
    lock = InstanceLock(config.lock_path)
    lock.acquire()
    stop_event = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    if install_signals:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop_event.set)
    server = TerminalServer(config, base_env=base_env, shell=shell)
    try:
        auth.load_or_create_token(config.token_path)
        write_rcfile(config.rcfile_path)
        config.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        await server.start()
        poller = server.spawn(server.manager.run(stop_event), name="terminal-poller")
        if ready is not None:
            ready(server)
        await stop_event.wait()
        poller.cancel()
    finally:
        log.info("terminal.stopping")
        await server.stop()
        with contextlib.suppress(FileNotFoundError):
            config.pid_path.unlink()
        if install_signals:
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(signum)
        lock.release()
        log.info("terminal.stopped")
