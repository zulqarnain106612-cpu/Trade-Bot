"""
Wire protocol of the terminal service: frame decoding and request validation.

Every frame is one JSON object with a string ``t`` (its type). Requests may
carry an ``id``; the reply to a request echoes it as ``{"t": "ok", "id": ...}``
or ``{"t": "error", "id": ..., "code": ..., "message": ...}``. Everything a
client sends is checked here before it reaches a PTY: sizes, identifiers,
names, signals, argv, working directories. Nothing a client sends is ever
interpreted by a shell -- input bytes go to the terminal the user is typing
into, and a job's argv goes straight to execve.

Output bytes travel base64-encoded so a frame is always valid JSON regardless
of what a program wrote; xterm.js and the CLI decode them back to the exact
bytes the PTY produced.

Registry: TERM-003 (config/quality_registry.json).
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import signal
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

SESSION_ID_RE = re.compile(r"^s-[0-9a-f]{8}$")
PROCESS_ID_RE = re.compile(r"^p-[0-9a-f]{8}$")

#: Signals a client may deliver. Deliberately short: these are the ones a
#: terminal user sends (interrupt, terminate, kill, hang up). STOP/CONT and
#: the user signals stay out because nothing in the UI needs them.
SIGNALS: Mapping[str, signal.Signals] = MappingProxyType(
    {
        "INT": signal.SIGINT,
        "TERM": signal.SIGTERM,
        "KILL": signal.SIGKILL,
        "HUP": signal.SIGHUP,
    }
)

CLIENT_KINDS = frozenset({"gui", "cli", "api"})

#: Request types any authenticated client may send.
CLIENT_REQUESTS = frozenset(
    {
        "hello",
        "ping",
        "session.create",
        "session.attach",
        "session.detach",
        "session.input",
        "session.resize",
        "session.rename",
        "session.signal",
        "session.close",
        "process.kill",
        "process.output",
    }
)

#: Job registration is for local programs (the trading API, scripts), never
#: for a browser: only the Unix socket accepts these.
JOB_REQUESTS = frozenset({"job.begin", "job.output", "job.end"})

MAX_NAME_CHARS = 64
MAX_ARGV_ITEMS = 256
MAX_ARG_CHARS = 4096
MAX_JOB_TEXT_CHARS = 8192
MAX_COLS = 1000
MAX_ROWS = 500

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ProtocolError(Exception):
    """A request the service refuses, with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def new_session_id() -> str:
    return f"s-{secrets.token_hex(4)}"


def new_process_id() -> str:
    return f"p-{secrets.token_hex(4)}"


def decode_frame(raw: str | bytes, max_bytes: int) -> dict[str, Any]:
    """Parse one frame, refusing oversized, non-JSON and non-object input."""
    size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    if size > max_bytes:
        raise ProtocolError("frame_too_large", f"frame of {size} bytes exceeds {max_bytes}")
    try:
        frame = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("bad_json", f"frame is not valid JSON: {exc}") from exc
    if not isinstance(frame, dict):
        raise ProtocolError("bad_frame", "frame must be a JSON object")
    kind = frame.get("t")
    if not isinstance(kind, str) or not kind:
        raise ProtocolError("bad_frame", "frame has no type")
    request_id = frame.get("id")
    if request_id is not None and not (
        isinstance(request_id, (str, int)) and not isinstance(request_id, bool)
    ):
        raise ProtocolError("bad_frame", "id must be a string or an integer")
    if isinstance(request_id, str) and len(request_id) > MAX_NAME_CHARS:
        raise ProtocolError("bad_frame", "id is too long")
    return frame


def encode_frame(frame: Mapping[str, Any]) -> str:
    return json.dumps(frame, separators=(",", ":"), ensure_ascii=False)


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def session_id(frame: Mapping[str, Any], key: str = "sid") -> str:
    value = frame.get(key)
    if not isinstance(value, str) or not SESSION_ID_RE.match(value):
        raise ProtocolError("bad_session_id", f"{key} must look like s-xxxxxxxx")
    return value


def process_id(frame: Mapping[str, Any], key: str = "process_id") -> str:
    value = frame.get(key)
    if not isinstance(value, str) or not PROCESS_ID_RE.match(value):
        raise ProtocolError("bad_process_id", f"{key} must look like p-xxxxxxxx")
    return value


def bounded_int(
    frame: Mapping[str, Any], key: str, lo: int, hi: int, default: int | None = None
) -> int:
    value = frame.get(key, default)
    if value is None:
        raise ProtocolError("bad_field", f"{key} is required")
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ProtocolError("bad_field", f"{key} must be an integer within [{lo}, {hi}]")
    return value


def optional_offset(frame: Mapping[str, Any], key: str = "since") -> int | None:
    value = frame.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError("bad_field", f"{key} must be a non-negative integer")
    return value


def text_field(frame: Mapping[str, Any], key: str, max_chars: int) -> str:
    value = frame.get(key)
    if not isinstance(value, str):
        raise ProtocolError("bad_field", f"{key} must be a string")
    if len(value) > max_chars:
        raise ProtocolError("too_large", f"{key} exceeds {max_chars} characters")
    return value


def validate_name(value: object) -> str:
    """A display name: 1-64 printable characters, surrounding space removed."""
    if not isinstance(value, str):
        raise ProtocolError("bad_name", "name must be a string")
    name = value.strip()
    if not name or len(name) > MAX_NAME_CHARS or _CONTROL_RE.search(name):
        raise ProtocolError("bad_name", f"name must be 1-{MAX_NAME_CHARS} printable characters")
    return name


def optional_name(frame: Mapping[str, Any]) -> str | None:
    return None if frame.get("name") is None else validate_name(frame["name"])


def validate_argv(value: object) -> list[str]:
    """A job's argv: a non-empty list of strings handed to execve unchanged."""
    if not isinstance(value, list) or not value or len(value) > MAX_ARGV_ITEMS:
        raise ProtocolError("bad_argv", f"argv must be a list of 1-{MAX_ARGV_ITEMS} strings")
    for item in value:
        if not isinstance(item, str) or len(item) > MAX_ARG_CHARS or "\x00" in item:
            raise ProtocolError("bad_argv", "every argv item must be a string without NUL")
    if not value[0]:
        raise ProtocolError("bad_argv", "argv[0] must name a program")
    return list(value)


def validate_cwd(value: object, default: Path) -> Path:
    """An absolute, existing directory, or *default* when none was given."""
    if value is None:
        return default
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ProtocolError("bad_cwd", "cwd must be a non-empty string")
    path = Path(value)
    if not path.is_absolute():
        raise ProtocolError("bad_cwd", "cwd must be an absolute path")
    if not path.is_dir():
        raise ProtocolError("bad_cwd", f"cwd is not a directory: {value}")
    return path


def signal_from(frame: Mapping[str, Any], default: str = "INT") -> signal.Signals:
    name = frame.get("signal", default)
    if not isinstance(name, str) or name.upper() not in SIGNALS:
        raise ProtocolError("bad_signal", f"signal must be one of {sorted(SIGNALS)}")
    return SIGNALS[name.upper()]
