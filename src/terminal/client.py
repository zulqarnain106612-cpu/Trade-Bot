"""
The blocking client the CLI uses.

One Unix-socket connection, newline-delimited JSON, requests correlated by
id. Frames that arrive while a request is outstanding (output, process
events) are kept, in order and bounded, for the caller to consume afterwards
-- nothing that arrives between a request and its reply is dropped.

Standard library only, on purpose: every CLI invocation imports this, and the
daemon's dependencies (asyncio, websockets, structlog) cost seconds of
startup on a slow machine for a command that sends one frame.

Registry: TERM-007 (config/quality_registry.json).
"""

from __future__ import annotations

import collections
import contextlib
import json
import socket
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from src.terminal import PROTOCOL_VERSION
from src.terminal.protocol import encode_frame

MAX_PENDING_EVENTS = 10000


class TerminalClientError(RuntimeError):
    """The service could not be reached or refused a request."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class TerminalClient:
    def __init__(self, socket_path: Path, *, timeout: float = 10.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.welcome: dict[str, Any] = {}
        self.snapshot: dict[str, Any] = {}
        self.events: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=MAX_PENDING_EVENTS
        )
        self._buffer = b""
        self._next_id = 0

    def connect(self, client: str = "cli") -> TerminalClient:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.socket_path))
        except OSError as exc:
            sock.close()
            raise TerminalClientError(
                f"the terminal service is not running ({self.socket_path}): {exc.strerror or exc}",
                code="unavailable",
            ) from exc
        self.sock = sock
        self.welcome = self.request("hello", protocol=PROTOCOL_VERSION, client=client)
        while True:
            frame = self.recv(self.timeout)
            if frame is None:
                raise TerminalClientError("the service closed the connection after hello")
            if frame.get("t") == "snapshot":
                self.snapshot = frame
                return self
            self.events.append(frame)

    def close(self) -> None:
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.close()
            self.sock = None

    def __enter__(self) -> TerminalClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def send(self, frame: dict[str, Any]) -> None:
        if self.sock is None:
            raise TerminalClientError("not connected")
        self.sock.sendall(encode_frame(frame).encode("utf-8") + b"\n")

    def request(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Send one request and wait for its reply; raise on an error reply."""
        self._next_id += 1
        request_id = self._next_id
        self.send({"t": kind, "id": request_id, **fields})
        while True:
            frame = self.recv(self.timeout)
            if frame is None:
                raise TerminalClientError(f"connection closed while waiting for {kind}")
            if frame.get("id") == request_id and frame.get("t") in ("ok", "welcome", "error"):
                if frame["t"] == "error":
                    raise TerminalClientError(
                        f"{frame.get('code')}: {frame.get('message')}", code=frame.get("code")
                    )
                return frame
            if frame.get("t") == "error" and frame.get("id") is None:
                raise TerminalClientError(
                    f"{frame.get('code')}: {frame.get('message')}", code=frame.get("code")
                )
            self.events.append(frame)

    def recv(self, timeout: float | None) -> dict[str, Any] | None:
        """The next frame, or None on EOF; raises TimeoutError on timeout."""
        if self.sock is None:
            raise TerminalClientError("not connected")
        while b"\n" not in self._buffer:
            self.sock.settimeout(timeout)
            chunk = self.sock.recv(1 << 16)
            if not chunk:
                return None
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        frame: dict[str, Any] = json.loads(line)
        return frame

    def has_buffered_frame(self) -> bool:
        return b"\n" in self._buffer

    def pop_events(self) -> Iterable[dict[str, Any]]:
        while self.events:
            yield self.events.popleft()
