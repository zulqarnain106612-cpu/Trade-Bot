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

One addition to that law, made deliberately: ``record()`` also publishes the
entry on the ``selftuning`` bus topic. It still does not interpret and it
still cannot refuse -- publishing is fire-and-forget by construction
(INV-032) -- but it is the only funnel every tuning event passes through.
The alternative was a publish at every call site in ``runner.py``,
``watchdog.py`` and the operator endpoints, which duplicates the contract
and silently omits whichever event type is added next.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from src.eventbus import get_event_bus


class TuningEventType(StrEnum):
    PROPOSED = "proposed"
    EVALUATED = "evaluated"
    WOULD_PROMOTE = "would_promote"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"
    PAUSED = "paused"
    RESUMED = "resumed"
    SKIPPED = "skipped"


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

    @property
    def path(self) -> Path:
        return self._path

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

        # Published after the write, outside the lock. After, because an event
        # the dashboard has seen but the audit log has not is a discrepancy an
        # incident review cannot explain. Outside, because publish() must not
        # be reachable while this holds a lock a concurrent recorder wants --
        # the bus is non-blocking, and keeping it that way means never giving
        # it a lock to be slow behind.
        #
        # A promotion or a rollback changes what the live system does with
        # real capital. Learning about it from a 15s poll is how an operator
        # ends up reading a parameter value that the bot had already moved on
        # from.
        get_event_bus().publish(
            "selftuning",
            {
                "param_name": entry.param_name,
                "event_type": entry.event_type.value,
                "timestamp": entry.timestamp,
                "details": entry.details,
            },
        )
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
