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
from collections import deque
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


# Entries retained in memory. The trail is written on every tick decision
# from signal_engine._emit_audit, so on three timeframes with a 1m stream
# this is thousands of entries a day, held by a process-wide singleton for
# the life of the process — an unbounded list that nothing ever freed.
#
# 100k is deliberately generous: weeks of history at that rate, so an
# operator reading the trail sees what they saw before, and only a run long
# enough to have OOMed under the old behaviour reaches the bound.
_MAX_RETAINED_ENTRIES: int = 100_000


@dataclass
class AuditTrail:
    """
    Hash-chained audit log, append-only with a bounded memory window.

    The chain is still append-only in the sense that matters: entries are
    never rewritten, sequence numbers never repeat, and each entry commits
    to its predecessor. What is bounded is how many are kept in RAM.

    Sequence and the chain head are tracked independently of the retained
    window, so eviction cannot renumber entries or break the link between
    the last evicted entry and the first retained one.
    """

    _entries: deque[AuditEntry] = field(
        default_factory=lambda: deque(maxlen=_MAX_RETAINED_ENTRIES)
    )
    # Next sequence number and the hash to chain from. Previously both were
    # derived from _entries, which is exactly what made the list impossible
    # to bound: dropping the head would have restarted numbering and
    # re-anchored the chain to genesis.
    _next_sequence: int = 0
    _last_hash: str = _GENESIS_HASH
    _evicted: int = 0

    def record(
        self,
        event_type: str,
        reason_code: str,
        details: dict[str, str | float | int | bool | None] | None = None,
        ts_ms: int | None = None,
    ) -> AuditEntry:
        # From the counters, not from the retained window: len() would
        # restart numbering once eviction begins, and [-1] would still be
        # correct only by luck.
        sequence = self._next_sequence
        prev_hash = self._last_hash
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
        if len(self._entries) == self._entries.maxlen:
            # deque drops the head silently on append; count it so nothing
            # downstream can report a complete chain it no longer holds.
            self._evicted += 1
        self._entries.append(entry)
        self._next_sequence += 1
        self._last_hash = entry_hash
        return entry

    def verify_chain_integrity(self) -> tuple[bool, int | None]:
        """
        Returns (intact, first_broken_sequence). Recomputes every retained
        entry's hash from its recorded fields and checks it matches both the
        stored hash and the next entry's prev_hash link.

        Verification starts from the oldest RETAINED entry's recorded
        prev_hash rather than from genesis. Once anything has been evicted
        the genesis anchor is gone, and walking from it would report a break
        at the first retained entry on every healthy trail — a check that
        fails on correct behaviour gets ignored. Callers that need to know
        how much of the chain is still covered should read evicted_count();
        the API surface reports it alongside this result rather than
        implying the whole history was verified.
        """
        prev_hash = self._entries[0].prev_hash if self._entries else _GENESIS_HASH
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

    def evicted_count(self) -> int:
        """
        Entries dropped from the retained window. 0 until the bound is hit.

        Exposed so a consumer can tell "the chain is intact" from "the part
        of the chain I still hold is intact", which are different claims.
        """
        return self._evicted

    def total_recorded(self) -> int:
        """Every entry ever recorded, including evicted ones."""
        return self._next_sequence

    def __len__(self) -> int:
        """Retained entries — not the total ever recorded, which is
        total_recorded(). len() is what the retained window costs in RAM."""
        return len(self._entries)


_audit_trail: AuditTrail = AuditTrail()


def get_audit_trail() -> AuditTrail:
    """Module-level singleton for the process-wide audit trail."""
    return _audit_trail
