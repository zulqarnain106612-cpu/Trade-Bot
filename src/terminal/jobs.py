"""
Registration of application jobs in the process center.

``JobReporter`` is what the trading application uses to put its own work --
a model retrain, a backfill -- in the same list as terminal commands. It is
deliberately incapable of affecting the job it reports:

* ``begin``/``output``/``end`` never block and never raise; they only queue;
* one background task sends the queued events in order over one connection;
* when the terminal service is not running -- the normal case on a server,
  where the API runs as a different user -- each event costs one failed
  connect to a Unix socket and is dropped.

The job id is generated here, so ``end`` can be queued before ``begin`` has
even been sent.

Registry: TERM-007 (config/quality_registry.json).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import structlog

from src.terminal import PROTOCOL_VERSION
from src.terminal.protocol import encode_frame, new_process_id

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class JobReporter:
    def __init__(
        self, socket_path: Path | None, *, timeout: float = 0.5, queue_size: int = 256
    ) -> None:
        self._socket_path = socket_path
        self._timeout = timeout
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._queue_size = queue_size
        self._worker: asyncio.Task[None] | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self.dropped = 0
        self.sent = 0

    def begin(self, name: str, detail: str | None = None) -> str:
        job_id = new_process_id()
        self._emit({"t": "job.begin", "process_id": job_id, "name": name[:64], "detail": detail})
        return job_id

    def output(self, job_id: str, text: str) -> None:
        self._emit({"t": "job.output", "process_id": job_id, "text": text[:8192]})

    def end(self, job_id: str, *, ok: bool, error: str | None = None) -> None:
        self._emit(
            {
                "t": "job.end",
                "process_id": job_id,
                "ok": ok,
                "error": None if error is None else error[:1024],
            }
        )

    def _emit(self, frame: dict[str, Any]) -> None:
        if self._socket_path is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.dropped += 1
            return
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._queue_size)
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            self.dropped += 1
            return
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._run(), name="terminal-job-reporter")

    async def _run(self) -> None:
        queue = self._queue
        if queue is None:  # pragma: no cover - _emit creates it before starting us
            return
        while not queue.empty():
            frame = queue.get_nowait()
            try:
                await asyncio.wait_for(self._send(frame), timeout=self._timeout)
                self.sent += 1
            except (OSError, TimeoutError, ValueError) as exc:
                self.dropped += 1
                log.debug("terminal.job_report_dropped", error=type(exc).__name__)
                await self._disconnect()

    async def _send(self, frame: dict[str, Any]) -> None:
        if self._writer is None:
            reader, writer = await asyncio.open_unix_connection(str(self._socket_path))
            self._reader, self._writer = reader, writer
            await self._exchange(
                {"t": "hello", "id": 0, "protocol": PROTOCOL_VERSION, "client": "api"}
            )
        await self._exchange({**frame, "id": 1})

    async def _exchange(self, frame: dict[str, Any]) -> None:
        if self._writer is None or self._reader is None:  # pragma: no cover - set by _send
            raise OSError("not connected")
        self._writer.write(encode_frame(frame).encode("utf-8") + b"\n")
        await self._writer.drain()
        while True:
            line = await self._reader.readline()
            if not line:
                raise OSError("the terminal service closed the connection")
            reply = json.loads(line)
            if reply.get("id") == frame["id"] and reply.get("t") in ("ok", "welcome", "error"):
                if reply["t"] == "error":
                    raise ValueError(str(reply.get("code")))
                return

    async def _disconnect(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def aclose(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        await self._disconnect()
