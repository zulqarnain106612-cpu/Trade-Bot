"""
``tradebot-term`` -- the host command line for the terminal service.

Every command talks to the same daemon the dashboard uses, so a session made
here shows up in the dashboard immediately and vice versa:

    tradebot-term start | stop | restart | status        the daemon
    tradebot-term ls | ps [--all]                         sessions, processes
    tradebot-term new [--name N] [--cwd D] [--attach]     a new shell
    tradebot-term attach SESSION                          Ctrl+] detaches
    tradebot-term output SESSION|PROCESS [--plain]        retained output
    tradebot-term send SESSION TEXT [--no-enter]          type into a session
    tradebot-term run [--name N] [--wait] -- CMD ...      a tracked job
    tradebot-term kill SESSION|PROCESS [--signal SIG]     signal one target
    tradebot-term rename SESSION NAME | close SESSION
    tradebot-term token [--rotate]                        browser access token
    tradebot-term install [--no-systemd] | uninstall [--purge]
    tradebot-term daemon                                  run in the foreground

Detaching never stops a session; ``close`` does, and so does stopping the
daemon. Output goes to stdout through ``sys.stdout`` -- src/ does not print().

Registry: TERM-007, TERM-008 (config/quality_registry.json).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from src.terminal import SERVICE_VERSION, auth, install
from src.terminal.client import TerminalClient, TerminalClientError
from src.terminal.config import REPO_ROOT, TerminalConfig, TerminalConfigError, ensure_private_dir
from src.terminal.protocol import PROCESS_ID_RE, SESSION_ID_RE, SIGNALS

# Only `daemon` needs asyncio, websockets, structlog and the trading logging
# setup; importing them here would put seconds of startup on every
# `tradebot-term ls`. They are imported inside cmd_daemon.

DETACH_BYTE = b"\x1d"  # Ctrl+]
EXIT_NOT_RUNNING = 3
LOG_ROTATE_BYTES = 5 << 20
_ANSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


class CliError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def _write(stream: TextIO, text: str) -> None:
    stream.write(text + "\n")
    stream.flush()


def _client(config: TerminalConfig) -> TerminalClient:
    try:
        return TerminalClient(config.socket_path).connect()
    except TerminalClientError as exc:
        raise CliError(f"{exc} -- start it with `tradebot-term start`", EXIT_NOT_RUNNING) from exc


def _ping(config: TerminalConfig) -> dict[str, Any] | None:
    try:
        with TerminalClient(config.socket_path, timeout=2.0).connect() as client:
            return client.welcome
    except (TerminalClientError, OSError):
        return None


def _resolve_session(client: TerminalClient, ref: str) -> str:
    if SESSION_ID_RE.match(ref):
        return ref
    matches = [s["id"] for s in client.snapshot.get("sessions", []) if s.get("name") == ref]
    if len(matches) != 1:
        raise CliError(f"no unique session named {ref!r}; use its id (tradebot-term ls)")
    return matches[0]


def _decode(frame: dict[str, Any]) -> bytes:
    return base64.b64decode(frame.get("data", ""))


def _elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _bytes(count: int | None) -> str:
    if count is None:
        return "-"
    for unit in ("B", "K", "M", "G"):
        if count < 1024 or unit == "G":
            return f"{count:.0f}{unit}" if unit == "B" else f"{count:.1f}{unit}"
        count /= 1024  # type: ignore[assignment]
    return "-"  # pragma: no cover - loop always returns


# -- daemon management --------------------------------------------------------


def _configure_daemon_logging() -> None:
    import structlog

    from src.logging_setup import redact_event

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact_event,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def cmd_daemon(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    import asyncio

    from src.terminal.server import DaemonError, run_daemon

    _configure_daemon_logging()
    try:
        asyncio.run(run_daemon(config, allow_root=args.allow_root))
    except DaemonError as exc:
        raise CliError(str(exc)) from exc
    return 0


def _unit_active(run: install.Runner) -> bool:
    return run(["systemctl", "--user", "is-active", "--quiet", install.UNIT_NAME]) == 0


def _unit_installed(env: dict[str, str]) -> bool:
    return install.is_managed(install.unit_dir(env) / install.UNIT_NAME)


def _spawn_detached(config: TerminalConfig) -> None:
    ensure_private_dir(config.state_dir)
    log_path = config.log_path
    if log_path.exists() and log_path.stat().st_size > LOG_ROTATE_BYTES:
        log_path.unlink()
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "src.terminal", "daemon"],
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=descriptor,
            stderr=descriptor,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(descriptor)


def ensure_running(
    config: TerminalConfig,
    *,
    run: install.Runner = install.run_quietly,
    spawn: Callable[[TerminalConfig], None] = _spawn_detached,
    timeout_s: float = 10.0,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Start the daemon if it is not answering; return its welcome frame."""
    welcome = _ping(config)
    if welcome is not None:
        return welcome
    environment = dict(os.environ if env is None else env)
    if _unit_installed(environment) and install.systemd_user_available(run):
        run(["systemctl", "--user", "start", install.UNIT_NAME])
    else:
        spawn(config)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        welcome = _ping(config)
        if welcome is not None:
            return welcome
        time.sleep(0.1)
    raise CliError(
        f"the terminal daemon did not come up within {timeout_s:.0f}s; see {config.log_path}"
    )


def _daemon_pid(config: TerminalConfig) -> int | None:
    try:
        pid = int(config.pid_path.read_text(encoding="utf-8").strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    # A stale pid file can name a recycled pid; only signal our own daemon.
    return pid if b"src.terminal" in cmdline else None


def stop_daemon(
    config: TerminalConfig, *, run: install.Runner = install.run_quietly, timeout_s: float = 20.0
) -> bool:
    """Stop the daemon (which closes every session). True if it was running."""
    if _ping(config) is None:
        return False
    if _unit_active(run):
        run(["systemctl", "--user", "stop", install.UNIT_NAME])
    else:
        pid = _daemon_pid(config)
        if pid is None:
            raise CliError("the daemon answers but its pid file is missing; stop it by hand")
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and config.socket_path.exists():
        time.sleep(0.1)
    return True


def cmd_start(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    welcome = ensure_running(config)
    info = {
        "socket": str(config.socket_path),
        "version": welcome.get("version"),
        "client_version": SERVICE_VERSION,
        "pid": welcome.get("server_pid"),
        "ws_url": f"ws://{config.ws_host}:{config.ws_port}/" if config.ws_port is not None else None,
    }
    if args.json:
        _write(out, json.dumps(info))
    elif not args.quiet:
        _write(out, f"terminal service running (pid {info['pid']}, version {info['version']})")
    return 0


def cmd_stop(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    was_running = stop_daemon(config)
    _write(out, "terminal service stopped" if was_running else "terminal service was not running")
    return 0


def cmd_restart(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    stop_daemon(config)
    welcome = ensure_running(config)
    _write(out, f"terminal service restarted (pid {welcome.get('server_pid')})")
    return 0


def cmd_status(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    welcome = _ping(config)
    if welcome is None:
        if args.json:
            _write(out, json.dumps({"running": False}))
        else:
            _write(out, "terminal service is not running")
        return EXIT_NOT_RUNNING
    with _client(config) as client:
        sessions = client.snapshot.get("sessions", [])
        active = client.snapshot.get("processes", {}).get("active", [])
    status = {
        "running": True,
        "pid": welcome.get("server_pid"),
        "version": welcome.get("version"),
        "outdated": welcome.get("version") != SERVICE_VERSION,
        "sessions": len(sessions),
        "active_processes": len(active),
        "socket": str(config.socket_path),
    }
    if args.json:
        _write(out, json.dumps(status))
    else:
        _write(
            out,
            f"running pid={status['pid']} version={status['version']} "
            f"sessions={status['sessions']} active={status['active_processes']}",
        )
        if status["outdated"]:
            _write(out, f"note: installed version is {SERVICE_VERSION}; run `tradebot-term restart`")
    return 0


# -- inspection -----------------------------------------------------------------


def cmd_ls(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    with _client(config) as client:
        sessions = client.snapshot.get("sessions", [])
    if args.json:
        _write(out, json.dumps(sessions))
        return 0
    _write(out, f"{'ID':<11} {'NAME':<16} {'KIND':<5} {'STATE':<8} {'PID':>7}  {'FOREGROUND':<24} CWD")
    for s in sessions:
        foreground = (s.get("foreground") or {}).get("title") or ""
        state = s["state"] if s["state"] == "running" else f"exit {s.get('exit_code', '?')}"
        _write(
            out,
            f"{s['id']:<11} {s['name'][:16]:<16} {s['kind']:<5} {state:<8} {s['pid']:>7}  "
            f"{foreground[:24]:<24} {s.get('cwd') or ''}",
        )
    return 0


def cmd_ps(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    with _client(config) as client:
        processes = client.snapshot.get("processes", {"active": [], "history": []})
    if args.json:
        _write(out, json.dumps(processes))
        return 0
    rows = list(processes.get("active", []))
    if args.all:
        rows += processes.get("history", [])
    _write(out, f"{'ID':<11} {'STATE':<9} {'EXIT':>5} {'PID':>7} {'SESSION':<11} {'TIME':>8} {'CPU%':>6} {'RSS':>7}  TITLE")
    for p in rows:
        elapsed = p.get("elapsed_s")
        if elapsed is None and p.get("ended_at") is not None:
            elapsed = p["ended_at"] - p["started_at"]
        exit_text = p.get("signal") or ("-" if p.get("exit_code") is None else str(p["exit_code"]))
        cpu = "-" if p.get("cpu_percent") is None else f"{p['cpu_percent']:.1f}"
        _write(
            out,
            f"{p['id']:<11} {p['state']:<9} {exit_text:>5} {p.get('pid') or '-':>7} "
            f"{p.get('session_id') or '-':<11} {_elapsed(elapsed):>8} {cpu:>6} "
            f"{_bytes(p.get('rss_bytes')):>7}  {p['title']}",
        )
    return 0


def _collect_session_output(client: TerminalClient, sid: str) -> bytes:
    reply = client.request("session.attach", sid=sid)
    offset, end = reply["offset"], reply["end"]
    chunks: list[bytes] = []
    position = offset
    for frame in client.pop_events():
        if frame.get("t") == "output" and frame.get("sid") == sid:
            chunks.append(_decode(frame))
            position = frame["offset"] + len(chunks[-1])
    while position < end:
        frame = client.recv(client.timeout)
        if frame is None:
            break
        if frame.get("t") == "output" and frame.get("sid") == sid:
            data = _decode(frame)
            chunks.append(data)
            position = frame["offset"] + len(data)
    client.request("session.detach", sid=sid)
    return b"".join(chunks)


def cmd_output(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    with _client(config) as client:
        if PROCESS_ID_RE.match(args.target):
            process = client.request("process.output", process_id=args.target)["process"]
            data = base64.b64decode(process.get("output", ""))
            header = (
                f"[{process['id']} {process['state']} exit={process.get('exit_code')} "
                f"signal={process.get('signal')} reason={process.get('failure_reason')}]"
            )
        else:
            data = _collect_session_output(client, _resolve_session(client, args.target))
            header = None
    if args.tail is not None:
        data = data[-args.tail :]
    if args.plain:
        data = _ANSI_RE.sub(b"", data).replace(b"\r\n", b"\n")
    if header and not args.plain:
        _write(out, header)
    out.write(data.decode("utf-8", errors="replace"))
    out.flush()
    return 0


# -- control --------------------------------------------------------------------


def _terminal_size(fd: int) -> tuple[int, int]:
    try:
        size = os.get_terminal_size(fd)
    except OSError:
        return 24, 80
    return max(1, size.lines), max(1, size.columns)


def cmd_new(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    rows, cols = _terminal_size(1)
    with _client(config) as client:
        fields: dict[str, Any] = {"rows": rows, "cols": cols}
        if args.name:
            fields["name"] = args.name
        if args.cwd:
            fields["cwd"] = str(Path(args.cwd).resolve())
        session = client.request("session.create", **fields)["session"]
        _write(out, session["id"])
        if args.attach:
            return attach(client, session["id"])
    return 0


def attach(
    client: TerminalClient,
    sid: str,
    *,
    stdin_fd: int | None = None,
    stdout_fd: int | None = None,
) -> int:
    """Mirror a session on this terminal until Ctrl+] (or the session ends)."""
    stdin_fd = sys.stdin.fileno() if stdin_fd is None else stdin_fd
    stdout_fd = sys.stdout.fileno() if stdout_fd is None else stdout_fd
    if os.environ.get("TB_TERMINAL_SESSION_ID") == sid:
        raise CliError("refusing to attach a session to itself")
    interactive = os.isatty(stdin_fd)
    saved = termios.tcgetattr(stdin_fd) if interactive else None
    resized = {"pending": True}
    previous_winch: Any = None
    if interactive:
        previous_winch = signal.signal(signal.SIGWINCH, lambda *_: resized.update(pending=True))
    client.request("session.attach", sid=sid)
    exited = False
    try:
        if interactive:
            tty.setraw(stdin_fd)
        while True:
            if interactive and resized["pending"]:
                resized["pending"] = False
                rows, cols = _terminal_size(stdout_fd)
                client.send({"t": "session.resize", "sid": sid, "rows": rows, "cols": cols})
            for frame in client.pop_events():
                exited = _show(frame, sid, stdout_fd) or exited
            if exited:
                _drain(client, sid, stdout_fd)
                break
            if client.has_buffered_frame():
                ready = [client.sock]
            else:
                ready, _, _ = select.select([stdin_fd, client.sock], [], [], 0.25)
            if client.sock in ready:
                frame = client.recv(None)
                if frame is None:
                    os.write(stdout_fd, b"\r\n[the terminal service closed the connection]\r\n")
                    return 1
                exited = _show(frame, sid, stdout_fd) or exited
            if stdin_fd in ready:
                data = os.read(stdin_fd, 4096)
                if not data:
                    # Piped input ended: show what it produced, then detach.
                    _drain(client, sid, stdout_fd, quiet_s=0.5)
                    break
                head, found, _tail = data.partition(DETACH_BYTE)
                if head:
                    client.send({"t": "session.input", "sid": sid, "data": head.decode("utf-8", errors="replace")})
                if found:
                    break
    finally:
        if saved is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        if interactive:
            signal.signal(signal.SIGWINCH, previous_winch or signal.SIG_DFL)
        with contextlib.suppress(TerminalClientError, OSError):
            client.send({"t": "session.detach", "sid": sid})
    note = "session ended" if exited else f"detached from {sid}; it keeps running"
    os.write(stdout_fd, f"\r\n[{note}]\r\n".encode())
    return 0


def _drain(client: TerminalClient, sid: str, stdout_fd: int, quiet_s: float = 0.2) -> None:
    """Show output still in flight when a session ended, until it goes quiet."""
    while client.has_buffered_frame() or select.select([client.sock], [], [], quiet_s)[0]:
        frame = client.recv(quiet_s)
        if frame is None:
            return
        _show(frame, sid, stdout_fd)


def _show(frame: dict[str, Any], sid: str, stdout_fd: int) -> bool:
    """Write an output frame; return True when the session has exited."""
    if frame.get("t") == "output" and frame.get("sid") == sid:
        os.write(stdout_fd, _decode(frame))
    elif frame.get("t") == "session" and frame["session"]["id"] == sid:
        return frame["session"]["state"] != "running"
    elif frame.get("t") == "session_removed" and frame.get("id") == sid:
        return True
    return False


def cmd_attach(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    with _client(config) as client:
        return attach(client, _resolve_session(client, args.session))


def cmd_send(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    text = " ".join(args.text) + ("" if args.no_enter else "\r")
    with _client(config) as client:
        client.request("session.input", sid=_resolve_session(client, args.session), data=text)
    return 0


def cmd_run(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise CliError("nothing to run: tradebot-term run -- CMD [ARGS...]")
    rows, cols = _terminal_size(1)
    fields: dict[str, Any] = {"argv": command, "rows": rows, "cols": cols, "attach": args.wait}
    fields["cwd"] = str(Path(args.cwd or os.getcwd()).resolve())
    if args.name:
        fields["name"] = args.name
    with _client(config) as client:
        session = client.request("session.create", **fields)["session"]
        job_id = session["job_entry"]
        if not args.wait:
            _write(out, f"{session['id']} {job_id}")
            return 0
        while True:
            for frame in client.pop_events():
                result = _job_frame(frame, session["id"], job_id, out)
                if result is not None:
                    return result
            frame = client.recv(None)
            if frame is None:
                raise CliError("the terminal service closed the connection")
            result = _job_frame(frame, session["id"], job_id, out)
            if result is not None:
                return result


def _job_frame(frame: dict[str, Any], sid: str, job_id: str, out: TextIO) -> int | None:
    if frame.get("t") == "output" and frame.get("sid") == sid:
        out.write(_decode(frame).decode("utf-8", errors="replace"))
        out.flush()
    elif frame.get("t") == "process_done" and frame["process"]["id"] == job_id:
        process = frame["process"]
        if process.get("signal"):
            return 128 + signal_number(process["signal"])
        return 1 if process.get("exit_code") is None else int(process["exit_code"])
    return None


def signal_number(name: str) -> int:
    """SIGKILL -> 9, and the SIG<n> spelling sessions.signal_name() uses for others."""
    try:
        return int(signal.Signals[name])
    except KeyError:
        digits = name.removeprefix("SIG")
        return int(digits) if digits.isdigit() else 0


def cmd_kill(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    with _client(config) as client:
        if PROCESS_ID_RE.match(args.target):
            reply = client.request("process.kill", process_id=args.target, signal=args.signal or "TERM")
        else:
            sid = _resolve_session(client, args.target)
            reply = client.request("session.signal", sid=sid, signal=args.signal or "INT")
    _write(out, f"signalled process group {reply['pgid']}")
    return 0


def cmd_rename(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    with _client(config) as client:
        client.request("session.rename", sid=_resolve_session(client, args.session), name=args.name)
    return 0


def cmd_close(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    with _client(config) as client:
        sid = _resolve_session(client, args.session)
        client.request("session.close", sid=sid)
        while True:
            frame = client.recv(client.timeout + config.limits.close_grace_s)
            if frame is None or (frame.get("t") == "session_removed" and frame.get("id") == sid):
                break
    _write(out, f"closed {sid}")
    return 0


# -- setup ---------------------------------------------------------------------


def cmd_token(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    ensure_private_dir(config.state_dir)
    value = auth.rotate_token(config.token_path) if args.rotate else auth.load_or_create_token(config.token_path)
    _write(out, value)
    return 0


def cmd_paths(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    paths = {
        "socket": str(config.socket_path),
        "runtime_dir": str(config.runtime_dir),
        "state_dir": str(config.state_dir),
        "token": str(config.token_path),
        "log": str(config.log_path),
        "ws_url": f"ws://{config.ws_host}:{config.ws_port}/" if config.ws_port is not None else None,
    }
    _write(out, json.dumps(paths) if args.json else "\n".join(f"{k}: {v}" for k, v in paths.items()))
    return 0


def cmd_install(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    report = install.install(
        python=Path(sys.executable), repo=REPO_ROOT, env=dict(os.environ), use_systemd=not args.no_systemd
    )
    return _report(report, out)


def cmd_uninstall(config: TerminalConfig, args: argparse.Namespace, out: TextIO) -> int:
    with contextlib.suppress(CliError):
        stop_daemon(config)
    report = install.uninstall(env=dict(os.environ), state_dir=config.state_dir, purge=args.purge)
    return _report(report, out)


def _report(report: install.InstallReport, out: TextIO) -> int:
    for path in report.written:
        _write(out, f"wrote {path}")
    for path in report.unchanged:
        _write(out, f"up to date {path}")
    for path in report.removed:
        _write(out, f"removed {path}")
    for note in report.notes:
        _write(out, f"note: {note}")
    for conflict in report.conflicts:
        _write(sys.stderr, f"conflict: {conflict}")
    return 0 if report.ok else 1


# -- entry point -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradebot-term", description="Trade-Bot terminal service")
    sub = parser.add_subparsers(dest="command", required=True)

    daemon = sub.add_parser("daemon", help="run the service in the foreground")
    daemon.add_argument("--allow-root", action="store_true")
    daemon.set_defaults(func=cmd_daemon)

    start = sub.add_parser("start", help="start the service if it is not running")
    start.add_argument("--json", action="store_true")
    start.add_argument("--quiet", action="store_true")
    start.set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop the service and every session").set_defaults(func=cmd_stop)
    sub.add_parser("restart", help="stop, then start").set_defaults(func=cmd_restart)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    ls = sub.add_parser("ls", help="list sessions")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_ls)
    ps = sub.add_parser("ps", help="list running processes (and history with --all)")
    ps.add_argument("--all", action="store_true")
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(func=cmd_ps)

    new = sub.add_parser("new", help="create a shell session")
    new.add_argument("--name")
    new.add_argument("--cwd")
    new.add_argument("--attach", action="store_true")
    new.set_defaults(func=cmd_new)
    attach_parser = sub.add_parser("attach", help="attach to a session (Ctrl+] detaches)")
    attach_parser.add_argument("session")
    attach_parser.set_defaults(func=cmd_attach)
    output = sub.add_parser("output", help="print a session's or process's retained output")
    output.add_argument("target")
    output.add_argument("--tail", type=int)
    output.add_argument("--plain", action="store_true")
    output.set_defaults(func=cmd_output)
    send = sub.add_parser("send", help="type text into a session")
    send.add_argument("session")
    send.add_argument("text", nargs="+")
    send.add_argument("--no-enter", action="store_true")
    send.set_defaults(func=cmd_send)
    run = sub.add_parser("run", help="run a command as a tracked job")
    run.add_argument("--name")
    run.add_argument("--cwd")
    run.add_argument("--wait", action="store_true")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)
    kill = sub.add_parser("kill", help="signal a session's foreground job or one process")
    kill.add_argument("target")
    kill.add_argument("--signal", choices=sorted(SIGNALS))
    kill.set_defaults(func=cmd_kill)
    rename = sub.add_parser("rename")
    rename.add_argument("session")
    rename.add_argument("name")
    rename.set_defaults(func=cmd_rename)
    close = sub.add_parser("close", help="terminate a session and everything in it")
    close.add_argument("session")
    close.set_defaults(func=cmd_close)

    token = sub.add_parser("token", help="print (or rotate) the browser access token")
    token.add_argument("--rotate", action="store_true")
    token.set_defaults(func=cmd_token)
    paths = sub.add_parser("paths")
    paths.add_argument("--json", action="store_true")
    paths.set_defaults(func=cmd_paths)
    install_parser = sub.add_parser("install", help="install the CLI wrapper and user service")
    install_parser.add_argument("--no-systemd", action="store_true")
    install_parser.set_defaults(func=cmd_install)
    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("--purge", action="store_true", help="also delete the token and state")
    uninstall.set_defaults(func=cmd_uninstall)
    return parser


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    stream = sys.stdout if out is None else out
    args = build_parser().parse_args(argv)
    try:
        config = TerminalConfig.from_env()
        return int(args.func(config, args, stream))
    except (CliError, TerminalConfigError, TerminalClientError, OSError) as exc:
        _write(sys.stderr, f"tradebot-term: {exc}")
        return exc.code if isinstance(exc, CliError) else 1
