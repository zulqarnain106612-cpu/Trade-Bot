"""
The one registry of running processes and jobs, and their recent outcomes.

Three kinds of entry share it, because the process center shows them in one
list:

* ``command`` -- what a terminal session's foreground job is running, with
  the actual process group read from the PTY and, when the shell integration
  reports it, the exact exit status;
* ``job`` -- a program started with ``tradebot-term run``, which is its own
  session and whose status comes from waitpid;
* ``app`` -- an operation the trading application registered (a retrain, a
  backfill), with the outcome it reported.

An entry is active until it finishes; then it moves, exactly once, into a
bounded history with its exit status, failure reason and the tail of its
output. State is derived from evidence only: exit 0 is ``succeeded``, any
other exit or a terminating signal is ``failed``, and a command whose status
nobody could observe is ``unknown`` -- never assumed to have succeeded.

Every change is reported through one callback, which the server turns into
pushed events; nothing polls this.

Registry: TERM-002 (config/quality_registry.json).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.terminal.protocol import ProtocolError, b64encode, new_process_id

STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_UNKNOWN = "unknown"

KINDS = frozenset({"command", "job", "app"})


@dataclass
class ProcessEntry:
    id: str
    kind: str
    title: str
    command: str | None
    session_id: str | None
    pid: int | None
    pgid: int | None
    cwd: str | None
    started_at: float
    started_mono: float
    state: str = STATE_RUNNING
    ended_at: float | None = None
    exit_code: int | None = None
    signal: str | None = None
    failure_reason: str | None = None
    cpu_percent: float | None = None
    rss_bytes: int | None = None
    process_count: int | None = None
    output_start: int | None = None
    output: bytes = b""
    output_truncated: bool = False
    detail: str | None = None

    def to_dict(
        self, *, include_output: bool = False, now_mono: float | None = None
    ) -> dict[str, Any]:
        elapsed = None
        if self.state == STATE_RUNNING and now_mono is not None:
            elapsed = max(0.0, now_mono - self.started_mono)
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "command": self.command,
            "session_id": self.session_id,
            "pid": self.pid,
            "pgid": self.pgid,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "elapsed_s": elapsed,
            "state": self.state,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "signal": self.signal,
            "failure_reason": self.failure_reason,
            "cpu_percent": self.cpu_percent,
            "rss_bytes": self.rss_bytes,
            "process_count": self.process_count,
            "detail": self.detail,
            "output_bytes": len(self.output),
        }
        if include_output:
            payload["output"] = b64encode(self.output)
            payload["output_truncated"] = self.output_truncated
        return payload


def outcome(
    exit_code: int | None, signal_name: str | None, reported_ok: bool | None = None
) -> tuple[str, str | None]:
    """
    The state and failure reason that the evidence supports.

    *reported_ok* is an application job's own account of how it ended; it is
    evidence too, but only for jobs that have no exit status to observe.
    """
    if reported_ok is not None and exit_code is None and signal_name is None:
        return (STATE_SUCCEEDED, None) if reported_ok else (STATE_FAILED, "reported failure")
    if signal_name is not None:
        return STATE_FAILED, f"terminated by {signal_name}"
    if exit_code is None:
        return STATE_UNKNOWN, None
    if exit_code == 0:
        return STATE_SUCCEEDED, None
    return STATE_FAILED, f"exited with status {exit_code}"


Listener = Callable[[str, dict[str, Any]], None]


class ProcessRegistry:
    def __init__(
        self,
        *,
        history_limit: int,
        output_limit: int,
        on_event: Listener | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._active: dict[str, ProcessEntry] = {}
        self._history: deque[ProcessEntry] = deque(maxlen=history_limit)
        self._output_limit = output_limit
        self._on_event = on_event
        self._clock = clock
        self._monotonic = monotonic

    # -- queries -----------------------------------------------------------

    def get(self, entry_id: str) -> ProcessEntry:
        entry = self._active.get(entry_id)
        if entry is not None:
            return entry
        for past in self._history:
            if past.id == entry_id:
                return past
        raise ProtocolError("not_found", f"no process {entry_id}")

    def active(self) -> list[ProcessEntry]:
        return list(self._active.values())

    def active_for_session(self, session_id: str) -> list[ProcessEntry]:
        return [e for e in self._active.values() if e.session_id == session_id]

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        now = self._monotonic()
        return {
            "active": [e.to_dict(now_mono=now) for e in self._active.values()],
            "history": [e.to_dict() for e in reversed(self._history)],
        }

    # -- lifecycle ---------------------------------------------------------

    def begin(
        self,
        *,
        kind: str,
        title: str,
        command: str | None = None,
        session_id: str | None = None,
        pid: int | None = None,
        pgid: int | None = None,
        cwd: str | None = None,
        output_start: int | None = None,
        entry_id: str | None = None,
        detail: str | None = None,
        started_at: float | None = None,
    ) -> ProcessEntry:
        if kind not in KINDS:
            raise ValueError(f"unknown process kind {kind!r}")
        new_id = entry_id or new_process_id()
        if new_id in self._active or any(e.id == new_id for e in self._history):
            raise ProtocolError("duplicate_id", f"process {new_id} already exists")
        entry = ProcessEntry(
            id=new_id,
            kind=kind,
            title=title,
            command=command,
            session_id=session_id,
            pid=pid,
            pgid=pgid,
            cwd=cwd,
            started_at=self._clock() if started_at is None else started_at,
            started_mono=self._monotonic(),
            output_start=output_start,
            detail=detail,
        )
        self._active[entry.id] = entry
        self._emit("process", entry)
        return entry

    def update(self, entry_id: str, **fields: Any) -> ProcessEntry:
        entry = self._active.get(entry_id)
        if entry is None:
            raise ProtocolError("not_running", f"process {entry_id} is not running")
        changed = False
        for key, value in fields.items():
            if not hasattr(entry, key):
                raise AttributeError(key)
            if getattr(entry, key) != value:
                setattr(entry, key, value)
                changed = True
        if changed:
            self._emit("process", entry)
        return entry

    def append_output(self, entry_id: str, data: bytes) -> None:
        """Keep the newest *output_limit* bytes of an app job's own output."""
        entry = self._active.get(entry_id)
        if entry is None:
            raise ProtocolError("not_running", f"process {entry_id} is not running")
        combined = entry.output + data
        if len(combined) > self._output_limit:
            combined = combined[-self._output_limit :]
            entry.output_truncated = True
        entry.output = combined
        self._emit("process", entry)

    def finish(
        self,
        entry_id: str,
        *,
        exit_code: int | None,
        signal_name: str | None = None,
        reason: str | None = None,
        output: bytes | None = None,
        output_truncated: bool = False,
        reported_ok: bool | None = None,
    ) -> ProcessEntry:
        entry = self._active.pop(entry_id, None)
        if entry is None:
            raise ProtocolError("not_running", f"process {entry_id} is not running")
        state, derived_reason = outcome(exit_code, signal_name, reported_ok)
        entry.state = state
        entry.exit_code = exit_code
        entry.signal = signal_name
        entry.failure_reason = reason if reason is not None else derived_reason
        entry.ended_at = self._clock()
        entry.cpu_percent = None
        if output is not None:
            if len(output) > self._output_limit:
                output = output[-self._output_limit :]
                output_truncated = True
            entry.output = output
            entry.output_truncated = output_truncated
        self._history.append(entry)
        self._emit("process_done", entry)
        return entry

    def _emit(self, kind: str, entry: ProcessEntry) -> None:
        if self._on_event is not None:
            self._on_event(kind, entry.to_dict(now_mono=self._monotonic()))
