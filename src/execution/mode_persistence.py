"""
EXEC-005 — the halt survives a restart.

The execution mode lives in `RuntimeConfig`, in memory, seeded from settings
at construction. That is correct for a flag and wrong for a kill switch: an
operator who disables trading and then watches the process get restarted by a
supervisor gets a bot that comes back trading, with the audit trail showing a
halt that is no longer in effect. Nobody is notified, because from the
process's point of view nothing failed.

So the mode is written to a small file whenever an operator changes it, and
read back at startup. Three properties make the file safe to trust:

  * the write is atomic (temp file + `os.replace`), so a crash mid-write
    leaves either the old mode or the new one, never half of a JSON document;
  * an unreadable, corrupt or unexpected file resolves to the most
    restrictive mode this bot has -- MANUAL, where every trade is queued for
    explicit operator approval -- not to the configured default. A state file
    we cannot parse is one we cannot rule out saying "halted", and the cost
    of being wrongly restricted is an operator clicking approve;
  * a *missing* file resolves to the configured default, because that is a
    first start rather than a lost state.

The asymmetry between the last two is the whole design. "No file" and
"unreadable file" look similar and mean opposite things.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Final

from src.config import ExecutionMode

# Overridable so tests and multi-instance deployments do not share one file.
ENV_PATH_VAR: Final[str] = "EXECUTION_MODE_STATE_PATH"
DEFAULT_STATE_PATH: Final[Path] = Path("data/execution_mode.json")

_SCHEMA_VERSION: Final[int] = 1

# The mode an unreadable state file resolves to. MANUAL rather than a
# hypothetical "off": this bot's most restrictive real mode is the one where
# nothing is placed without a human, which is what a halt means here.
FAIL_CLOSED_MODE: Final[ExecutionMode] = ExecutionMode.MANUAL


def state_path() -> Path:
    override = os.environ.get(ENV_PATH_VAR, "").strip()
    return Path(override) if override else DEFAULT_STATE_PATH


def save_execution_mode(mode: ExecutionMode, path: Path | None = None) -> None:
    """
    Persist *mode* atomically.

    The temp file is created in the destination directory, not in /tmp:
    `os.replace` is only atomic within a filesystem, and a cross-device move
    silently degrades to copy-then-delete -- which is exactly the
    non-atomicity this is here to avoid.
    """
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": _SCHEMA_VERSION, "execution_mode": mode.value})

    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".execution_mode-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            # The rename is atomic but the *contents* are not durable until
            # fsync; without it a power loss can leave a renamed, empty file.
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        # Leaving a stray temp file behind would eventually fill the
        # directory with one per failed write.
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


def load_execution_mode(default: ExecutionMode, path: Path | None = None) -> ExecutionMode:
    """
    Read the persisted mode, falling back per the rules in the module docstring.

    Returns *default* only when the file is absent. Every other failure --
    unreadable, malformed, unknown mode, wrong schema version -- resolves to
    `FAIL_CLOSED_MODE`.
    """
    target = path or state_path()
    if not target.exists():
        return default
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != _SCHEMA_VERSION:
            return FAIL_CLOSED_MODE
        return ExecutionMode(raw["execution_mode"])
    except (OSError, ValueError, KeyError, TypeError):
        return FAIL_CLOSED_MODE


def clear_execution_mode(path: Path | None = None) -> None:
    """Remove the state file. For an operator resetting to configured default."""
    target = path or state_path()
    with suppress(OSError):
        target.unlink()
