"""
Who may talk to the terminal service.

Two doors, two locks:

* The Unix socket lives in a 0700 directory and is itself 0600, and every
  connection's peer credentials (SO_PEERCRED) must carry this daemon's uid.
  The CLI, the Electron main process and the trading API use it.
* The loopback WebSocket exists only for a dashboard served to an ordinary
  browser. It requires a 256-bit token, checked in constant time, which only
  a process able to read the 0600 token file can know. The token is sent in
  the first frame, never in a URL, so it does not land in history or logs.

The token authorizes the terminal and nothing else: it is not an API key and
no API key opens the terminal.

Registry: TERM-004, SEC-0010 (config/quality_registry.json).
"""

from __future__ import annotations

import hmac
import os
import secrets
import socket
import stat
import struct
from pathlib import Path

TOKEN_BYTES = 32
_PEERCRED = struct.Struct("3i")


class TokenFileError(OSError):
    """The token file exists but cannot be trusted or read."""


def _read(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise TokenFileError(f"{path} is not a regular file owned by this user")
    if info.st_mode & 0o077:
        # Someone widened it. Narrow it again before trusting the content.
        os.chmod(path, 0o600)
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < TOKEN_BYTES * 2:
        raise TokenFileError(f"{path} does not hold a full-length token")
    return value


def _write(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    os.replace(temporary, path)


def load_or_create_token(path: Path) -> str:
    """Return the existing token, creating one (0600) if there is none."""
    if path.exists():
        return _read(path)
    value = secrets.token_hex(TOKEN_BYTES)
    _write(path, value)
    return value


def rotate_token(path: Path) -> str:
    value = secrets.token_hex(TOKEN_BYTES)
    _write(path, value)
    return value


def token_accepted(path: Path, presented: object) -> bool:
    """
    Constant-time comparison against the token file as it is now.

    Read per check, so ``tradebot-term token --rotate`` takes effect for the
    next connection without restarting the daemon. Any failure to read the
    file denies.
    """
    if not isinstance(presented, str) or not presented:
        return False
    try:
        expected = _read(path)
    except OSError:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def peer_uid(sock: socket.socket) -> int | None:
    """The uid of the process at the other end of a Unix socket (Linux)."""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:  # pragma: no cover - non-Linux; the 0700 directory still applies
        return None
    raw = sock.getsockopt(socket.SOL_SOCKET, option, _PEERCRED.size)
    _pid, uid, _gid = _PEERCRED.unpack(raw)
    return uid
