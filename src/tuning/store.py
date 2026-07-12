"""
Versioned, append-only config store for the self-tuning subsystem.

Design: docs/SELF_TUNING_DESIGN.md §5 ("never regress" via rollback).

Every promotion (and every rollback) is appended as a new record -- prior
records are never edited or deleted. This gives a full audit trail for
free and makes rollback a pure "read the previous record" operation, not
a destructive undo.

This store is the durable record; it is not read directly by the live
trading path. TuningRunner.attempt() also advances the in-memory
ParameterRegistry champion value on promotion, and
src/tuning/live_overrides.py reads that registry to overlay promoted
values onto the Settings the live regime/risk/features/model code
consumes -- see that module for the actual live wiring. This store's role
is durability and audit history: bootstrap.py's register_* functions seed
a fresh registry entry from the latest record here (when one exists,
within operator-anchored bounds) so a process restart resumes from the
last promotion instead of the original .env default.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class NoVersionsError(LookupError):
    """Raised when history/current is requested for a parameter with no recorded versions."""


class NoPriorVersionError(LookupError):
    """Raised when rollback is requested but there is no earlier version to revert to."""


@dataclass(frozen=True)
class ConfigVersion:
    """One immutable record in a parameter's version history."""

    param_name: str
    value: float
    version: int
    timestamp: str
    promoted_by: str
    evidence: dict[str, Any]
    is_rollback: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> ConfigVersion:
        return ConfigVersion(
            param_name=d["param_name"],
            value=d["value"],
            version=d["version"],
            timestamp=d["timestamp"],
            promoted_by=d["promoted_by"],
            evidence=d["evidence"],
            is_rollback=d.get("is_rollback", False),
        )


class VersionedConfigStore:
    """
    Append-only, per-parameter version history, backed by a JSON-lines file.

    Thread-safe. The full history is replayed into memory at construction
    time (version logs for a bounded whitelist of tuning parameters stay
    small -- this is not a high-write-volume system by design, see the
    rate-limiting invariant in the design doc).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._history: dict[str, list[ConfigVersion]] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = ConfigVersion.from_dict(json.loads(line))
                self._history.setdefault(record.param_name, []).append(record)

    def _append_to_disk(self, record: ConfigVersion) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(record.to_json() + "\n")

    def current(self, param_name: str) -> ConfigVersion:
        with self._lock:
            versions = self._history.get(param_name)
            if not versions:
                raise NoVersionsError(param_name)
            return versions[-1]

    def history(self, param_name: str) -> list[ConfigVersion]:
        with self._lock:
            return list(self._history.get(param_name, []))

    def has_versions(self, param_name: str) -> bool:
        with self._lock:
            return bool(self._history.get(param_name))

    def promote(
        self,
        param_name: str,
        value: float,
        evidence: dict[str, Any],
        promoted_by: str = "bot",
    ) -> ConfigVersion:
        """Append a new champion value. `evidence` should carry the metrics
        diff that justified promotion (see PromotionGate, Phase 2)."""
        with self._lock:
            existing = self._history.setdefault(param_name, [])
            next_version = (existing[-1].version + 1) if existing else 1
            record = ConfigVersion(
                param_name=param_name,
                value=value,
                version=next_version,
                timestamp=datetime.now(UTC).isoformat(),
                promoted_by=promoted_by,
                evidence=evidence,
            )
            existing.append(record)
            self._append_to_disk(record)
            return record

    def rollback(self, param_name: str) -> ConfigVersion:
        """
        Revert to the value two versions back by appending a new record
        (marked is_rollback=True) that copies the prior value forward --
        never deletes or edits history.
        """
        with self._lock:
            existing = self._history.get(param_name)
            if not existing or len(existing) < 2:
                raise NoPriorVersionError(param_name)
            prior = existing[-2]
            next_version = existing[-1].version + 1
            record = ConfigVersion(
                param_name=param_name,
                value=prior.value,
                version=next_version,
                timestamp=datetime.now(UTC).isoformat(),
                promoted_by="watchdog",
                evidence={"reason": "auto-rollback", "reverted_to_version": prior.version},
                is_rollback=True,
            )
            existing.append(record)
            self._append_to_disk(record)
            return record
