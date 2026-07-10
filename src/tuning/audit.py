"""
Immutable audit trail for self-tuning attempts.

Design: docs/SELF_TUNING_DESIGN.md §1.5 ("Full audit trail" invariant).

Every attempt the self-tuning subsystem makes -- proposed, evaluated,
promoted, rejected, or auto-rolled-back -- is recorded here. This is a
compliance requirement for a trading system: an operator (or Claude, or
a future incident review) must be able to reconstruct exactly what the
bot tried, when, and why a decision went the way it did.

This module intentionally does not interpret or act on events -- it is a
write-append, read-only-after-write log. Decision logic lives in the
proposer/evaluator/gate (Phase 2).
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any


class TuningEventType(str, Enum):
    PROPOSED = "proposed"
    EVALUATED = "evaluated"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"
    PAUSED = "paused"
    RESUMED = "resumed"


@dataclass(frozen=True)
class TuningAuditEntry:
    param_name: str
    event_type: TuningEventType
    timestamp: str
    details: dict[str, Any]

    def to_json(self) -> str:
        d = asdict(self)
        d["event_type"] = self.event_type.value
        return json.dumps(d, sort_keys=True)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> TuningAuditEntry:
        return TuningAuditEntry(
            param_name=d["param_name"],
            event_type=TuningEventType(d["event_type"]),
            timestamp=d["timestamp"],
            details=d["details"],
        )


class TuningAuditLog:
    """
    Append-only, thread-safe audit log backed by a JSON-lines file.

    No update/delete API is exposed by design -- the audit trail must
    remain tamper-evident. If a record needs correction, a new record
    referencing it should be appended instead.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def record(
        self,
        param_name: str,
        event_type: TuningEventType,
        details: dict[str, Any] | None = None,
    ) -> TuningAuditEntry:
        entry = TuningAuditEntry(
            param_name=param_name,
            event_type=event_type,
            timestamp=datetime.now(UTC).isoformat(),
            details=details or {},
        )
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(entry.to_json() + "\n")
        return entry

    def read_all(self) -> list[TuningAuditEntry]:
        with self._lock:
            if not self._path.exists():
                return []
            entries: list[TuningAuditEntry] = []
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entries.append(TuningAuditEntry.from_dict(json.loads(line)))
            return entries

    def read_for_param(self, param_name: str) -> list[TuningAuditEntry]:
        return [e for e in self.read_all() if e.param_name == param_name]
