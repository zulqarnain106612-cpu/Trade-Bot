"""
Terminal service configuration: where its files live, what it listens on, and
the bounds it enforces.

Read from ``TB_TERMINAL_*`` environment variables rather than src/config.py's
Settings on purpose. Settings loads the trading .env -- exchange keys, the
operator secret -- and the terminal daemon has no reason to hold any of them.
A daemon that only ever parses its own variables cannot leak what it never
read.

Every value is validated here, once. In particular the WebSocket listener
refuses any non-loopback address: the terminal executes commands as the
logged-in user, and "reachable from the network" is not a configuration
mistake this module will let anyone make by accident.

Registry: TERM-004 (config/quality_registry.json).
"""

from __future__ import annotations

import ipaddress
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: The repository this package was loaded from. Sessions start here unless a
#: client asks for another directory.
REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_WS_HOST = "127.0.0.1"
DEFAULT_WS_PORT = 8766
#: The Vite dev server is the only browser origin the dashboard is served from
#: (frontend/vite.config.js). Electron does not use the WebSocket at all.
DEFAULT_ALLOWED_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

_ENV_PREFIX = "TB_TERMINAL_"
#: sockaddr_un.sun_path is 108 bytes on Linux, including the terminating NUL.
MAX_SOCKET_PATH_BYTES = 107


class TerminalConfigError(ValueError):
    """A configuration value is invalid or a path is not safe to use."""


@dataclass(frozen=True)
class Limits:
    """Every bound the service enforces, in one place so tests can shrink them."""

    max_sessions: int = 12
    max_clients: int = 32
    buffer_bytes: int = 1 << 20
    history_entries: int = 100
    history_output_bytes: int = 64 * 1024
    max_frame_bytes: int = 1 << 20
    max_input_chars: int = 64 * 1024
    send_queue_frames: int = 2048
    close_grace_s: float = 2.0
    poll_interval_s: float = 0.5
    hello_timeout_s: float = 5.0


@dataclass(frozen=True)
class TerminalConfig:
    """Resolved configuration for one daemon or one client."""

    runtime_dir: Path
    state_dir: Path
    default_cwd: Path = REPO_ROOT
    ws_host: str = DEFAULT_WS_HOST
    #: None disables the WebSocket listener entirely (Electron and the CLI use
    #: the Unix socket and do not need it); TB_TERMINAL_WS_PORT=0 means None.
    #: A literal 0 binds an ephemeral port, which only tests construct.
    ws_port: int | None = DEFAULT_WS_PORT
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    limits: Limits = field(default_factory=Limits)

    @property
    def socket_path(self) -> Path:
        return self.runtime_dir / "termd.sock"

    @property
    def lock_path(self) -> Path:
        return self.runtime_dir / "termd.lock"

    @property
    def pid_path(self) -> Path:
        return self.runtime_dir / "termd.pid"

    @property
    def token_path(self) -> Path:
        return self.state_dir / "token"

    @property
    def rcfile_path(self) -> Path:
        return self.state_dir / "bash-integration.sh"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "termd.log"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TerminalConfig:
        """Build the configuration from the environment, validating every value."""
        source = os.environ if env is None else env
        uid = os.getuid()

        runtime_override = source.get(_ENV_PREFIX + "RUNTIME_DIR", "").strip()
        if runtime_override:
            runtime_dir = Path(runtime_override)
        elif source.get("XDG_RUNTIME_DIR", "").strip():
            runtime_dir = Path(source["XDG_RUNTIME_DIR"]) / "tradebot-terminal"
        else:
            # The per-user fallback lives in a shared directory, which is why
            # ensure_private_dir() refuses one that somebody else created.
            runtime_dir = Path("/tmp") / f"tradebot-terminal-{uid}"

        state_override = source.get(_ENV_PREFIX + "STATE_DIR", "").strip()
        if state_override:
            state_dir = Path(state_override)
        elif source.get("XDG_STATE_HOME", "").strip():
            state_dir = Path(source["XDG_STATE_HOME"]) / "tradebot-terminal"
        else:
            home = Path(source.get("HOME", str(Path.home())))
            state_dir = home / ".local/state/tradebot-terminal"

        for name, path in (("RUNTIME_DIR", runtime_dir), ("STATE_DIR", state_dir)):
            if not path.is_absolute():
                raise TerminalConfigError(f"{_ENV_PREFIX}{name} must be an absolute path: {path}")
        socket_bytes = len(os.fsencode(runtime_dir / "termd.sock"))
        if socket_bytes > MAX_SOCKET_PATH_BYTES:
            # bind() fails with a bare "AF_UNIX path too long" deep inside the
            # daemon otherwise; say which setting to shorten instead.
            raise TerminalConfigError(
                f"socket path under {runtime_dir} is {socket_bytes} bytes; Unix sockets allow "
                f"{MAX_SOCKET_PATH_BYTES}. Set {_ENV_PREFIX}RUNTIME_DIR to a shorter directory."
            )

        cwd_override = source.get(_ENV_PREFIX + "DEFAULT_CWD", "").strip()
        default_cwd = Path(cwd_override) if cwd_override else REPO_ROOT
        if not default_cwd.is_absolute():
            raise TerminalConfigError(
                f"{_ENV_PREFIX}DEFAULT_CWD must be an absolute path: {default_cwd}"
            )

        ws_host = source.get(_ENV_PREFIX + "WS_HOST", DEFAULT_WS_HOST).strip() or DEFAULT_WS_HOST
        validate_loopback_host(ws_host)
        ws_port = _int_from(source, "WS_PORT", DEFAULT_WS_PORT, 0, 65535) or None

        origins_raw = source.get(_ENV_PREFIX + "ALLOWED_ORIGINS", "").strip()
        origins = (
            tuple(o.strip() for o in origins_raw.split(",") if o.strip())
            if origins_raw
            else DEFAULT_ALLOWED_ORIGINS
        )
        for origin in origins:
            if origin == "*" or origin.lower() == "null":
                # "null" is what a sandboxed iframe on any website sends, so it
                # is as permissive as the wildcard.
                raise TerminalConfigError(f"origin {origin!r} would admit any website")

        buffer_bytes = _int_from(source, "BUFFER_BYTES", Limits.buffer_bytes, 1 << 16, 16 << 20)
        limits = Limits(
            max_sessions=_int_from(source, "MAX_SESSIONS", Limits.max_sessions, 1, 64),
            buffer_bytes=buffer_bytes,
            history_entries=_int_from(source, "HISTORY", Limits.history_entries, 1, 1000),
        )
        return cls(
            runtime_dir=runtime_dir,
            state_dir=state_dir,
            default_cwd=default_cwd,
            ws_host=ws_host,
            ws_port=ws_port,
            allowed_origins=origins,
            limits=limits,
        )


def _int_from(source: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = source.get(_ENV_PREFIX + name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise TerminalConfigError(f"{_ENV_PREFIX}{name} must be an integer, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise TerminalConfigError(f"{_ENV_PREFIX}{name} must be within [{lo}, {hi}], got {value}")
    return value


def validate_loopback_host(host: str) -> str:
    """Return *host* if it is a loopback IP literal; raise otherwise."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise TerminalConfigError(
            f"terminal listener host must be a loopback IP literal, got {host!r}"
        ) from exc
    if not address.is_loopback:
        raise TerminalConfigError(
            f"terminal listener refuses non-loopback address {host!r}: the terminal "
            "runs commands as this user and is never exposed to the network"
        )
    return host


def ensure_private_dir(path: Path) -> Path:
    """
    Create *path* as a directory only this user can enter, or refuse it.

    A directory that already exists is accepted only if it is a real directory
    (not a symlink) owned by this uid. Group/other bits are stripped from one
    we own; one owned by anybody else is refused, because a pre-created
    /tmp/tradebot-terminal-<uid> is exactly how another local user would plant
    a socket for the CLI to talk to.
    """
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TerminalConfigError(f"{path} is not a real directory")
    if info.st_uid != os.getuid():
        raise TerminalConfigError(f"{path} is owned by uid {info.st_uid}, not this user")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path
