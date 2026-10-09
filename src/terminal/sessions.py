"""
PTY sessions: spawn, stream, resize, signal, observe and clean up.

Each session is a real pseudo-terminal. The child gets a new session and the
slave as its controlling terminal (TIOCSCTTY), so the kernel's line
discipline does what a terminal emulator relies on: Ctrl+C is SIGINT to the
foreground process group, Ctrl+D is EOF on an empty line, TIOCSWINSZ sends
SIGWINCH, and job control works. Nothing here emulates any of that.

What runs in a session is observed, not inferred:

* the foreground process group is ``tcgetpgrp(master)``; its argv, cwd and
  start time come from /proc;
* with the bash integration, each command's text and exact exit status come
  from the shell's own OSC 133 markers;
* a job started with an argv is the session leader, and its status is the
  one waitpid reports.

Cleanup is deterministic: closing a session hangs up its process groups,
waits a bounded grace period for everything in the session to leave, then
SIGKILLs whatever remains in that session id -- so closing a session, or
stopping the daemon, leaves no orphaned process behind it.

Registry: TERM-001, TERM-002, TERM-006 (config/quality_registry.json).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import structlog

from src.terminal import procfs
from src.terminal.buffer import OutputBuffer
from src.terminal.config import TerminalConfig
from src.terminal.markers import Marker, MarkerScanner
from src.terminal.protocol import ProtocolError, new_session_id
from src.terminal.registry import ProcessRegistry
from src.terminal.shell_integration import shell_argv

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

READ_CHUNK = 65536
MAX_PENDING_INPUT = 1 << 20
#: After a session's leader exits, output still buffered in the PTY is read
#: before the outcome is recorded; this bounds the wait when a background
#: process keeps the terminal open.
FINALIZE_GRACE_S = 1.0
#: Exited sessions stay listed (their output is still useful) up to this many.
MAX_EXITED_SESSIONS = 20

SessionListener = Callable[[str, dict[str, Any]], None]
OutputListener = Callable[[str, int, bytes], None]


def _acquire_controlling_terminal() -> None:  # pragma: no cover - runs in the forked child
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def set_window_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def spawn_pty(
    argv: list[str], *, cwd: str, env: Mapping[str, str], rows: int, cols: int
) -> tuple[subprocess.Popen[bytes], int]:
    """Start *argv* on a new PTY; return the process and the master fd."""
    master, slave = pty.openpty()
    try:
        set_window_size(master, rows, cols)
        process = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=dict(env),
            start_new_session=True,
            preexec_fn=_acquire_controlling_terminal,
            close_fds=True,
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    os.set_blocking(master, False)
    return process, master


def signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"SIG{signum}"


class PtySession:
    """One terminal: its process, its PTY, its output and what it is running."""

    def __init__(
        self,
        *,
        sid: str,
        name: str,
        kind: str,
        argv: list[str],
        process: subprocess.Popen[bytes],
        master_fd: int,
        rows: int,
        cols: int,
        cwd: str,
        buffer_bytes: int,
        created_at: float,
    ) -> None:
        self.id = sid
        self.name = name
        self.kind = kind
        self.argv = argv
        self.process = process
        self.pid = process.pid
        self.master_fd = master_fd
        self.rows = rows
        self.cols = cols
        self.initial_cwd = cwd
        self.created_at = created_at
        self.buffer = OutputBuffer(buffer_bytes)
        self.scanner = MarkerScanner()
        self.state = "running"
        self.exit_code: int | None = None
        self.signal_name: str | None = None
        self.integration = False
        self.command_text: str | None = None
        self.command_entry: str | None = None
        self.job_entry: str | None = None
        self.foreground: dict[str, Any] | None = None
        self.eof = False
        self.exited_mono: float | None = None
        self.finalized = False
        self.pending_input = bytearray()
        self.writer_registered = False
        self.fd_open = True

    def to_dict(self, proc_root: Path = procfs.PROC) -> dict[str, Any]:
        cwd = procfs.read_cwd(self.pid, proc_root) if self.state == "running" else None
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "program": Path(self.argv[0]).name,
            "argv": self.argv if self.kind == "job" else None,
            "pid": self.pid,
            "rows": self.rows,
            "cols": self.cols,
            "cwd": cwd or self.initial_cwd,
            "created_at": self.created_at,
            "state": self.state,
            "exit_code": self.exit_code,
            "signal": self.signal_name,
            "integration": self.integration,
            "foreground": self.foreground,
            "output_end": self.buffer.end,
            "command_entry": self.command_entry,
            "job_entry": self.job_entry,
        }


class SessionManager:
    def __init__(
        self,
        config: TerminalConfig,
        registry: ProcessRegistry,
        *,
        on_session: SessionListener | None = None,
        on_output: OutputListener | None = None,
        base_env: Mapping[str, str] | None = None,
        shell: str | None = None,
        proc_root: Path = procfs.PROC,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._limits = config.limits
        self._registry = registry
        self._on_session = on_session
        self._on_output = on_output
        self._base_env = dict(os.environ if base_env is None else base_env)
        self._shell = shell or self._base_env.get("SHELL") or "/bin/bash"
        self._proc = proc_root
        self._clock = clock
        self._monotonic = monotonic
        self._sessions: dict[str, PtySession] = {}
        self._closing: dict[str, asyncio.Future[None]] = {}
        self._cpu_samples: dict[str, tuple[float, int]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._counter = 0

    # -- queries -----------------------------------------------------------

    def get(self, sid: str) -> PtySession:
        session = self._sessions.get(sid)
        if session is None:
            raise ProtocolError("not_found", f"no session {sid}")
        return session

    def sessions(self) -> list[PtySession]:
        return list(self._sessions.values())

    def describe(self, sid: str) -> dict[str, Any]:
        return self.get(sid).to_dict(self._proc)

    def snapshot(self) -> list[dict[str, Any]]:
        return [s.to_dict(self._proc) for s in self._sessions.values()]

    def running_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.state == "running")

    # -- lifecycle ---------------------------------------------------------

    async def create(
        self,
        *,
        name: str | None = None,
        cwd: Path | None = None,
        rows: int = 24,
        cols: int = 80,
        argv: list[str] | None = None,
    ) -> PtySession:
        if self.running_count() >= self._limits.max_sessions:
            raise ProtocolError(
                "too_many_sessions", f"at most {self._limits.max_sessions} sessions may run at once"
            )
        await self._prune_exited()
        self._loop = asyncio.get_running_loop()

        sid = new_session_id()
        while sid in self._sessions:  # pragma: no cover - 32-bit collision
            sid = new_session_id()
        kind = "job" if argv is not None else "shell"
        if argv is not None:
            command = list(argv)
        else:
            command = shell_argv(self._shell, self._config.rcfile_path)
        workdir = str(cwd or self._config.default_cwd)
        try:
            process, master = spawn_pty(
                command, cwd=workdir, env=self._child_env(sid), rows=rows, cols=cols
            )
        except OSError as exc:
            raise ProtocolError(
                "spawn_failed", f"could not start {command[0]}: {exc.strerror or exc}"
            ) from exc

        self._counter += 1
        default_name = (
            f"{Path(command[0]).name} {self._counter}"
            if kind == "shell"
            else " ".join(command)[:64]
        )
        session = PtySession(
            sid=sid,
            name=name or default_name,
            kind=kind,
            argv=command,
            process=process,
            master_fd=master,
            rows=rows,
            cols=cols,
            cwd=workdir,
            buffer_bytes=self._limits.buffer_bytes,
            created_at=self._clock(),
        )
        self._sessions[sid] = session
        self._loop.add_reader(master, self._on_readable, session)
        if kind == "job":
            entry = self._registry.begin(
                kind="job",
                title=" ".join(command),
                command=" ".join(command),
                session_id=sid,
                pid=process.pid,
                pgid=process.pid,
                cwd=workdir,
                output_start=0,
                detail=session.name,
            )
            session.job_entry = entry.id
        log.info("terminal.session_created", session=sid, kind=kind, pid=process.pid)
        self._emit_session(session)
        return session

    def write(self, sid: str, data: bytes) -> None:
        session = self._running(sid)
        if session.pending_input:
            self._queue_input(session, data)
            return
        try:
            written = self._write_fd(session.master_fd, data)
        except BlockingIOError:
            written = 0
        except OSError as exc:
            detail = f"could not write to {sid}: {exc.strerror}"
            raise ProtocolError("write_failed", detail) from exc
        if written < len(data):
            self._queue_input(session, data[written:])

    def resize(self, sid: str, rows: int, cols: int) -> None:
        session = self._running(sid)
        set_window_size(session.master_fd, rows, cols)
        session.rows, session.cols = rows, cols
        self._emit_session(session)

    def rename(self, sid: str, name: str) -> None:
        session = self.get(sid)
        session.name = name
        if session.job_entry is not None:
            self._registry.update(session.job_entry, detail=name)
        self._emit_session(session)

    def signal_foreground(self, sid: str, signum: int) -> int:
        """Deliver *signum* to the session's foreground process group only."""
        session = self._running(sid)
        try:
            group = os.tcgetpgrp(session.master_fd)
            os.killpg(group, signum)
        except OSError as exc:
            raise ProtocolError("signal_failed", f"could not signal {sid}: {exc.strerror}") from exc
        return group

    def kill_process(self, entry_id: str, signum: int) -> int:
        """
        Signal one registered process group, after proving it is still ours.

        Between the registry recording a group and an operator clicking
        "kill", the group may have exited and its id been reused by an
        unrelated process. The group is signalled only if it still belongs to
        the session that started it.
        """
        entry = self._registry.get(entry_id)
        if entry.state != "running":
            raise ProtocolError("not_running", f"process {entry_id} has already finished")
        if entry.pgid is None or entry.session_id is None:
            raise ProtocolError("not_killable", f"process {entry_id} has no process group")
        session = self._sessions.get(entry.session_id)
        if session is None or session.state != "running":
            raise ProtocolError("not_running", f"the session of {entry_id} has ended")
        try:
            owner = os.getsid(entry.pgid)
        except ProcessLookupError as exc:
            raise ProtocolError("not_running", f"process group {entry.pgid} is gone") from exc
        if owner != session.pid:
            detail = f"process group {entry.pgid} left session {session.id}"
            raise ProtocolError("not_owned", detail)
        try:
            os.killpg(entry.pgid, signum)
        except OSError as exc:
            detail = f"could not signal {entry_id}: {exc.strerror}"
            raise ProtocolError("signal_failed", detail) from exc
        log.info("terminal.process_signalled", process=entry_id, signal=signal_name(signum))
        return entry.pgid

    async def close(self, sid: str) -> None:
        """Terminate everything in the session, release the PTY and forget it."""
        session = self.get(sid)
        pending = self._closing.get(sid)
        if pending is not None:
            await pending
            return
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._closing[sid] = done
        try:
            # Also when the leader has already exited: a background child can
            # outlive it in the same session and must not outlive the close.
            if session.state == "running" or procfs.session_members(
                procfs.scan(self._proc), session.pid
            ):
                await self._terminate(session)
            self._check_exit(session)
            session.eof = True
            self._finalize(session, force=True)
            self._release(session)
            self._sessions.pop(sid, None)
            log.info("terminal.session_closed", session=sid)
            if self._on_session is not None:
                self._on_session("session_removed", {"id": sid})
        finally:
            del self._closing[sid]
            done.set_result(None)

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.close(sid) for sid in list(self._sessions)))

    # -- observation -------------------------------------------------------

    def poll_once(self) -> None:
        """Reap exits, track foreground jobs and sample resource use."""
        now = self._monotonic()
        for session in list(self._sessions.values()):
            if session.state == "running":
                self._check_exit(session)
            if session.state == "running":
                self._refresh_foreground(session)
            self._finalize(session)
        if any(entry.pgid is not None for entry in self._registry.active()):
            self._sample_usage(procfs.scan(self._proc), now)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.poll_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._limits.poll_interval_s)

    # -- internals ---------------------------------------------------------

    def _running(self, sid: str) -> PtySession:
        session = self.get(sid)
        if session.state != "running":
            raise ProtocolError("session_exited", f"session {sid} has exited")
        return session

    def _child_env(self, sid: str) -> dict[str, str]:
        env = dict(self._base_env)
        if "npm_lifecycle_event" in env:
            # Inherited from an `npm run` that launched the dashboard: npm's
            # own settings would silently change how npm behaves in here.
            for key in [k for k in env if k.startswith("npm_")]:
                del env[key]
            env.pop("INIT_CWD", None)
        for key in ("ELECTRON_RUN_AS_NODE", "ELECTRON_NO_ATTACH_CONSOLE"):
            env.pop(key, None)
        env.update(
            {
                "TERM": "xterm-256color",
                "COLORTERM": "truecolor",
                "TERM_PROGRAM": "tradebot-terminal",
                "TB_TERMINAL_SESSION_ID": sid,
            }
        )
        return env

    @staticmethod
    def _write_fd(fd: int, data: bytes) -> int:
        """The one place input reaches a PTY (a seam for back-pressure tests)."""
        return os.write(fd, data)

    def _queue_input(self, session: PtySession, data: bytes) -> None:
        if len(session.pending_input) + len(data) > MAX_PENDING_INPUT:
            raise ProtocolError("input_backlog", f"session {session.id} is not reading its input")
        session.pending_input += data
        if not session.writer_registered and self._loop is not None:
            self._loop.add_writer(session.master_fd, self._flush_input, session)
            session.writer_registered = True

    def _flush_input(self, session: PtySession) -> None:
        try:
            written = self._write_fd(session.master_fd, bytes(session.pending_input))
        except BlockingIOError:
            return
        except OSError:
            written = len(session.pending_input)
        del session.pending_input[:written]
        if not session.pending_input and self._loop is not None:
            self._loop.remove_writer(session.master_fd)
            session.writer_registered = False

    def _on_readable(self, session: PtySession) -> None:
        try:
            data = os.read(session.master_fd, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            # EIO: every slave descriptor is closed, which is how a PTY says
            # end-of-output.
            data = b""
        if not data:
            self._on_eof(session)
            return
        offset = session.buffer.append(data)
        if self._on_output is not None:
            self._on_output(session.id, offset, data)
        for marker in session.scanner.feed(data, offset):
            self._apply_marker(session, marker)

    def _on_eof(self, session: PtySession) -> None:
        session.eof = True
        if self._loop is not None and session.fd_open:
            self._loop.remove_reader(session.master_fd)
        self._check_exit(session)
        self._finalize(session)

    def _apply_marker(self, session: PtySession, marker: Marker) -> None:
        session.integration = True
        if marker.kind == "command":
            session.command_text = marker.text
        elif marker.kind == "start":
            if session.command_entry is not None:
                self._end_command(session, exit_code=None, until=marker.start)
            text, session.command_text = session.command_text, None
            entry = self._registry.begin(
                kind="command",
                title=text or "(command)",
                command=text,
                session_id=session.id,
                cwd=procfs.read_cwd(session.pid, self._proc),
                output_start=marker.end,
            )
            session.command_entry = entry.id
            self._refresh_foreground(session)
        elif session.command_entry is not None:  # "end"
            self._end_command(session, exit_code=marker.status, until=marker.start)

    def _end_command(
        self,
        session: PtySession,
        *,
        exit_code: int | None,
        until: int | None = None,
        reason: str | None = None,
    ) -> None:
        entry_id = session.command_entry
        if entry_id is None:
            return
        session.command_entry = None
        entry = self._registry.get(entry_id)
        start = entry.output_start if entry.output_start is not None else session.buffer.end
        end = session.buffer.end if until is None else until
        output, truncated = session.buffer.slice(start, end)
        self._cpu_samples.pop(entry_id, None)
        self._registry.finish(
            entry_id,
            exit_code=exit_code,
            reason=reason,
            output=output,
            output_truncated=truncated,
        )

    def _check_exit(self, session: PtySession) -> None:
        if session.state != "running":
            return
        code = session.process.poll()
        if code is None:
            return
        session.state = "exited"
        if code < 0:
            session.signal_name = signal_name(-code)
        else:
            session.exit_code = code
        session.exited_mono = self._monotonic()
        session.foreground = None
        log.info(
            "terminal.session_exited",
            session=session.id,
            exit_code=session.exit_code,
            signal=session.signal_name,
        )
        self._emit_session(session)

    def _finalize(self, session: PtySession, *, force: bool = False) -> None:
        """Record the session's outcome once its output has been drained."""
        if session.state != "exited" or session.finalized:
            return
        exited = session.exited_mono if session.exited_mono is not None else self._monotonic()
        if not (force or session.eof or self._monotonic() - exited >= FINALIZE_GRACE_S):
            return
        session.finalized = True
        self._end_command(
            session,
            exit_code=None,
            reason="the shell exited before the command reported a status",
        )
        if session.job_entry is not None:
            output, truncated = session.buffer.slice(0, session.buffer.end)
            self._cpu_samples.pop(session.job_entry, None)
            self._registry.finish(
                session.job_entry,
                exit_code=session.exit_code,
                signal_name=session.signal_name,
                output=output,
                output_truncated=truncated,
            )
        self._emit_session(session)

    def _refresh_foreground(self, session: PtySession) -> None:
        if session.kind == "job":
            return
        try:
            group = os.tcgetpgrp(session.master_fd)
        except OSError:
            return
        if group <= 0 or group == session.pid:
            if session.foreground is not None:
                session.foreground = None
                self._emit_session(session)
            if not session.integration and session.command_entry is not None:
                # Without the integration nobody can see the status: the shell
                # reaped the job. Reported as unknown rather than as success.
                self._end_command(session, exit_code=None)
            return

        argv = procfs.read_cmdline(group, self._proc)
        title = " ".join(argv) if argv else None
        cwd = procfs.read_cwd(group, self._proc)
        if session.command_entry is not None:
            entry = self._registry.get(session.command_entry)
            if not session.integration and entry.pgid not in (None, group):
                self._end_command(session, exit_code=None)
        if session.command_entry is None:
            if session.integration:
                # The command's start marker has not been read yet; the next
                # poll attaches this group to it.
                return
            entry = self._registry.begin(
                kind="command",
                title=title or f"process group {group}",
                session_id=session.id,
                pid=group,
                pgid=group,
                cwd=cwd,
                output_start=session.buffer.end,
                started_at=self._start_time(group),
            )
            session.command_entry = entry.id
        else:
            current = self._registry.get(session.command_entry)
            self._registry.update(
                session.command_entry,
                pid=group,
                pgid=group,
                title=title or current.title,
                cwd=cwd or current.cwd,
            )
        foreground = {"pgid": group, "title": title}
        if foreground != session.foreground:
            session.foreground = foreground
            self._emit_session(session)

    def _start_time(self, pid: int) -> float | None:
        stat = procfs.read_stat(pid, self._proc)
        booted = procfs.boot_time(self._proc)
        if stat is None or booted is None:
            return None
        return booted + stat.start_ticks / procfs.clock_ticks()

    def _sample_usage(self, stats: list[procfs.ProcStat], now: float) -> None:
        hertz = procfs.clock_ticks()
        for entry in self._registry.active():
            if entry.pgid is None:
                continue
            usage = procfs.group_usage(stats, entry.pgid)
            if usage is None:
                continue
            previous = self._cpu_samples.get(entry.id)
            self._cpu_samples[entry.id] = (now, usage.cpu_ticks)
            cpu = entry.cpu_percent
            if previous is not None and now > previous[0]:
                ticks = max(0, usage.cpu_ticks - previous[1])
                cpu = round(100.0 * ticks / hertz / (now - previous[0]), 1)
            self._registry.update(
                entry.id, cpu_percent=cpu, rss_bytes=usage.rss_bytes, process_count=usage.count
            )

    async def _terminate(self, session: PtySession) -> None:
        with contextlib.suppress(OSError):
            group = os.tcgetpgrp(session.master_fd)
            if group > 0 and group != session.pid:
                os.killpg(group, signal.SIGHUP)
                os.killpg(group, signal.SIGCONT)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(session.pid, signal.SIGHUP)

        deadline = self._monotonic() + self._limits.close_grace_s
        while self._monotonic() < deadline:
            session.process.poll()
            if not procfs.session_members(procfs.scan(self._proc), session.pid):
                break
            await asyncio.sleep(0.02)
        for pid in procfs.session_members(procfs.scan(self._proc), session.pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

        # SIGKILL delivery is asynchronous. Do not report the session closed
        # while a surviving member is still runnable; otherwise callers can
        # observe a process that the close operation promised to terminate.
        kill_deadline = self._monotonic() + max(self._limits.close_grace_s, 1.0)
        while self._monotonic() < kill_deadline:
            members = procfs.session_members(procfs.scan(self._proc), session.pid)
            if not any(
                (stat := procfs.read_stat(pid, self._proc)) is not None
                and stat.comm
                and stat.comm != " "
                for pid in members
            ):
                break
            await asyncio.sleep(0.01)

        if session.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                session.process.kill()
            # A SIGKILLed process is reaped as soon as the kernel delivers it.
            for _ in range(100):
                if session.process.poll() is not None:
                    break
                await asyncio.sleep(0.01)

    def _release(self, session: PtySession) -> None:
        if not session.fd_open:
            return
        if self._loop is not None:
            self._loop.remove_reader(session.master_fd)
            if session.writer_registered:
                self._loop.remove_writer(session.master_fd)
                session.writer_registered = False
        os.close(session.master_fd)
        session.fd_open = False

    async def _prune_exited(self) -> None:
        exited = [s for s in self._sessions.values() if s.state == "exited"]
        for session in exited[: max(0, len(exited) - MAX_EXITED_SESSIONS + 1)]:
            await self.close(session.id)

    def _emit_session(self, session: PtySession) -> None:
        if self._on_session is not None:
            self._on_session("session", session.to_dict(self._proc))
