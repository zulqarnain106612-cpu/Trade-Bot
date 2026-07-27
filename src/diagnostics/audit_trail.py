"""
Immutable audit trail — v8 Institutional-Grade Operations & Compliance.

Append-only, in-process log of every order, signal, and risk decision with
a machine-readable reason code, so any historical decision can be
reconstructed without relying on scattered log lines. "Immutable" here
means append-only and hash-chained (each entry commits to the previous
entry's hash) so tampering with history is detectable, not that it uses
any exotic storage — persistence to durable storage is the caller's
responsibility (e.g. periodic flush to the existing storage backend).

Authority:
  - Domain Prior: no hidden failures or skipped validation — every risk
    decision must be reconstructable after the fact
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One immutable, hash-chained audit record."""

    sequence: int
    ts_ms: int
    event_type: str  # e.g. "order_placed", "signal_generated", "risk_gate_fired"
    reason_code: str
    details: dict[str, str | float | int | bool | None]
    prev_hash: str
    entry_hash: str


def _compute_entry_hash(
    sequence: int,
    ts_ms: int,
    event_type: str,
    reason_code: str,
    details: dict[str, str | float | int | bool | None],
    prev_hash: str,
) -> str:
    payload = json.dumps(
        {
            "sequence": sequence,
            "ts_ms": ts_ms,
            "event_type": event_type,
            "reason_code": reason_code,
            "details": details,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_GENESIS_HASH: str = "0" * 64


@dataclass
class AuditTrail:
    """Append-only, hash-chained audit log."""

    _entries: list[AuditEntry] = field(default_factory=list)

    def record(
        self,
        event_type: str,
        reason_code: str,
        details: dict[str, str | float | int | bool | None] | None = None,
        ts_ms: int | None = None,
    ) -> AuditEntry:
        sequence = len(self._entries)
        prev_hash = self._entries[-1].entry_hash if self._entries else _GENESIS_HASH
        resolved_ts = ts_ms if ts_ms is not None else int(datetime.now(tz=UTC).timestamp() * 1000)
        resolved_details = details or {}

        entry_hash = _compute_entry_hash(
            sequence, resolved_ts, event_type, reason_code, resolved_details, prev_hash
        )
        entry = AuditEntry(
            sequence=sequence,
            ts_ms=resolved_ts,
            event_type=event_type,
            reason_code=reason_code,
            details=resolved_details,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )
        self._entries.append(entry)
        return entry

    def verify_chain_integrity(self) -> tuple[bool, int | None]:
        """
        Returns (intact, first_broken_sequence). Recomputes every entry's
        hash from its recorded fields and checks it matches both the
        stored hash and the next entry's prev_hash link.
        """
        prev_hash = _GENESIS_HASH
        for entry in self._entries:
            expected = _compute_entry_hash(
                entry.sequence,
                entry.ts_ms,
                entry.event_type,
                entry.reason_code,
                entry.details,
                prev_hash,
            )
            if expected != entry.entry_hash or entry.prev_hash != prev_hash:
                return False, entry.sequence
            prev_hash = entry.entry_hash
        return True, None

    def entries(self) -> tuple[AuditEntry, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


_audit_trail: AuditTrail = AuditTrail()


def get_audit_trail() -> AuditTrail:
    """Module-level singleton for the process-wide audit trail."""
    return _audit_trail
