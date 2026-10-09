"""
Shared helpers for the terminal service tests (fixtures live in conftest.py).

Everything here runs against real PTYs and a real Unix socket inside a
short temporary directory -- the suite's network guard leaves AF_UNIX alone,
and sun_path is limited to 107 bytes, which a tmp_path carrying the test's
own name can exceed. Waiting is always on an event the service raises, never
on a clock.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from src.terminal import PROTOCOL_VERSION
from src.terminal.config import Limits, TerminalConfig
from src.terminal.registry import ProcessRegistry
from src.terminal.server import TerminalServer, run_daemon
from src.terminal.sessions import SessionManager

FAST_LIMITS = Limits(
    max_sessions=4,
    max_clients=4,
    buffer_bytes=64 * 1024,
    history_entries=10,
    history_output_bytes=4096,
    max_frame_bytes=64 * 1024,
    max_input_chars=4096,
    send_queue_frames=256,
    close_grace_s=2.0,
    poll_interval_s=0.02,
    hello_timeout_s=2.0,
)

WAIT_S = 15.0


def make_config(base: Path, **overrides: Any) -> TerminalConfig:
    runtime = base / "run"
    state = base / "state"
    runtime.mkdir(mode=0o700, exist_ok=True)
    state.mkdir(mode=0o700, exist_ok=True)
    params: dict[str, Any] = {
        "runtime_dir": runtime,
        "state_dir": state,
        "default_cwd": base,
        "ws_port": None,
        "limits": FAST_LIMITS,
    }
    params.update(overrides)
    return TerminalConfig(**params)


def shell_env(home: Path) -> dict[str, str]:
    """A minimal environment: no developer .bashrc, no inherited npm/electron vars."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LANG": "C.UTF-8"}


def alive(pid: int) -> bool:
    """True while *pid* exists and is not a zombie waiting to be reaped."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat[stat.rfind(")") + 2] not in ("Z", "X")


class Changes:
    """Wakes a waiting test whenever the service reports anything."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def notify(self, *_args: Any) -> None:
        self._event.set()

    async def until(self, predicate: Callable[[], Any], timeout: float = WAIT_S) -> Any:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            result = predicate()
            if result:
                return result
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError("the expected state was never reported")
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), remaining)
            except TimeoutError as exc:
                raise AssertionError("the expected state was never reported") from exc


class Harness:
    """A SessionManager wired to recorders, with its poller running."""

    def __init__(
        self,
        config: TerminalConfig,
        home: Path,
        shell: str = "/bin/bash",
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.changes = Changes()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.output: dict[str, bytearray] = {}
        self.registry = ProcessRegistry(
            history_limit=config.limits.history_entries,
            output_limit=config.limits.history_output_bytes,
            on_event=self._record,
        )
        self.manager = SessionManager(
            config,
            self.registry,
            on_session=self._record,
            on_output=self._output,
            base_env=env if env is not None else shell_env(home),
            shell=shell,
        )
        self.stop = asyncio.Event()
        self.poller = asyncio.ensure_future(self.manager.run(self.stop))

    def _record(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))
        self.changes.notify()

    def _output(self, sid: str, _offset: int, data: bytes) -> None:
        self.output.setdefault(sid, bytearray()).extend(data)
        self.changes.notify()

    def text(self, sid: str) -> str:
        return bytes(self.output.get(sid, b"")).decode("utf-8", errors="replace")

    def finished(self, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        for entry in self.registry.snapshot()["history"]:
            if predicate(entry):
                return entry
        return None

    def running(self, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        for entry in self.registry.snapshot()["active"]:
            if predicate(entry):
                return entry
        return None

    async def wait_finished(self, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        return await self.changes.until(lambda: self.finished(predicate))

    async def wait_running(self, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        return await self.changes.until(lambda: self.running(predicate))

    async def shell(self, **kwargs: Any) -> Any:
        """A bash session whose integration has announced itself."""
        session = await self.manager.create(**kwargs)
        await self.changes.until(lambda: session.integration)
        return session

    async def type(self, sid: str, text: str) -> None:
        self.manager.write(sid, text.encode("utf-8"))

    async def close(self) -> None:
        self.stop.set()
        await self.poller
        await self.manager.shutdown()


class AsyncClient:
    """A minimal protocol client over the Unix socket, for in-loop tests."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.frames: list[dict[str, Any]] = []
        self.snapshot: dict[str, Any] = {}
        self._next = 100

    @classmethod
    async def connect(cls, path: Path, *, hello: bool = True, client: str = "cli") -> AsyncClient:
        reader, writer = await asyncio.open_unix_connection(str(path), limit=1 << 22)
        conn = cls(reader, writer)
        if hello:
            welcome = await conn.request("hello", protocol=PROTOCOL_VERSION, client=client)
            assert welcome["t"] == "welcome", welcome
            conn.snapshot = await conn.wait_for(lambda f: f["t"] == "snapshot")
        return conn

    async def send(self, frame: dict[str, Any] | bytes) -> None:
        raw = frame if isinstance(frame, bytes) else json.dumps(frame).encode()
        self.writer.write(raw + b"\n")
        await self.writer.drain()

    async def recv(self, timeout: float = WAIT_S) -> dict[str, Any] | None:
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        if not line:
            return None
        frame = json.loads(line)
        self.frames.append(frame)
        return frame

    async def wait_for(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float = WAIT_S
    ) -> Any:
        for frame in list(self.frames):
            if predicate(frame):
                self.frames.remove(frame)
                return frame
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            frame = await self.recv(max(0.01, deadline - loop.time()))
            if frame is None:
                raise AssertionError("connection closed before the expected frame")
            if predicate(frame):
                self.frames.remove(frame)
                return frame

    async def request(self, kind: str, **fields: Any) -> dict[str, Any]:
        self._next += 1
        request_id = self._next
        await self.send({"t": kind, "id": request_id, **fields})
        return await self.wait_for(
            lambda f: f.get("id") == request_id and f["t"] in ("ok", "welcome", "error")
        )

    async def closed(self, timeout: float = WAIT_S) -> bool:
        """True once the daemon has closed this connection."""
        while True:
            frame = await self.recv(timeout)
            if frame is None:
                return True

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()


class Daemon:
    """run_daemon() as a task in the test's own loop."""

    def __init__(self, config: TerminalConfig, home: Path, **kwargs: Any) -> None:
        self.config = config
        self.stop = asyncio.Event()
        self.ready = asyncio.Event()
        self.server: TerminalServer | None = None
        self.task = asyncio.ensure_future(
            run_daemon(
                config,
                stop=self.stop,
                install_signals=kwargs.pop("install_signals", False),
                ready=self._ready,
                base_env=shell_env(home),
                shell="/bin/bash",
                **kwargs,
            )
        )

    def _ready(self, server: TerminalServer) -> None:
        self.server = server
        self.ready.set()

    async def started(self) -> Daemon:
        waiter = asyncio.ensure_future(self.ready.wait())
        done, _ = await asyncio.wait(
            {waiter, self.task}, timeout=WAIT_S, return_when=asyncio.FIRST_COMPLETED
        )
        if self.task in done:
            waiter.cancel()
            self.task.result()  # re-raise why the daemon did not start
        assert waiter in done, "the daemon never became ready"
        return self

    async def shutdown(self) -> None:
        self.stop.set()
        await asyncio.wait_for(self.task, WAIT_S)


class ThreadedDaemon:
    """The daemon in its own thread and loop, for the blocking CLI client."""

    def __init__(self, config: TerminalConfig, home: Path) -> None:
        self.config = config
        self._home = home
        self._ready = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop: asyncio.Event | None = None
        self.server: TerminalServer | None = None
        self.error: Exception | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def main() -> None:
            self.loop = asyncio.get_running_loop()
            self.stop = asyncio.Event()

            def ready(server: TerminalServer) -> None:
                self.server = server
                self._ready.set()

            await run_daemon(
                self.config,
                stop=self.stop,
                install_signals=False,
                ready=ready,
                base_env=shell_env(self._home),
                shell="/bin/bash",
            )

        try:
            asyncio.run(main())
        except Exception as exc:  # surfaced to the test by start()
            self.error = exc
            self._ready.set()

    def start(self) -> ThreadedDaemon:
        self.thread.start()
        assert self._ready.wait(WAIT_S), "the daemon never became ready"
        if self.error is not None:
            raise self.error
        return self

    def shutdown(self) -> None:
        if self.loop is not None and self.stop is not None and self.thread.is_alive():
            self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(WAIT_S)
