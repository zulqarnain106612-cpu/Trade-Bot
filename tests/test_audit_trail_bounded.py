"""
The audit trail must be bounded in memory without breaking its chain.

signal_engine records to the process-wide trail on every tick decision, so
on three timeframes with a 1m stream it grew by thousands of entries a day
and nothing ever freed them. Bounding it is only safe if eviction cannot
renumber entries or re-anchor the chain — both of which the original
derivation from len(_entries) and _entries[-1] would have done.
"""

from __future__ import annotations

import dataclasses
from collections import deque

from src.diagnostics.audit_trail import AuditTrail


def _trail(maxlen: int) -> AuditTrail:
    return AuditTrail(_entries=deque(maxlen=maxlen))


def test_the_retained_window_is_bounded() -> None:
    trail = _trail(5)
    for i in range(12):
        trail.record("tick", "ok", {"i": i})

    assert len(trail) == 5
    assert trail.total_recorded() == 12
    assert trail.evicted_count() == 7


def test_sequence_numbers_do_not_restart_on_eviction() -> None:
    # Derived from len(_entries) they would have, silently reusing sequence
    # numbers already committed to by evicted entries' hashes.
    trail = _trail(5)
    for i in range(12):
        trail.record("tick", "ok", {"i": i})

    assert [e.sequence for e in trail.entries()] == [7, 8, 9, 10, 11]


def test_the_chain_links_across_an_eviction_boundary() -> None:
    trail = _trail(3)
    for i in range(8):
        trail.record("tick", "ok", {"i": i})

    entries = trail.entries()
    for earlier, later in zip(entries, entries[1:], strict=True):
        assert later.prev_hash == earlier.entry_hash


def test_a_truncated_chain_still_verifies() -> None:
    # Anchoring verification at genesis would report a break at the first
    # retained entry on every healthy trail — a check that fails on correct
    # behaviour gets ignored rather than fixed.
    trail = _trail(4)
    for i in range(20):
        trail.record("tick", "ok", {"i": i})

    intact, first_broken = trail.verify_chain_integrity()
    assert intact is True
    assert first_broken is None


def test_tampering_inside_the_retained_window_is_still_detected() -> None:
    trail = _trail(6)
    for i in range(6):
        trail.record("tick", "ok", {"i": i})

    trail._entries[2] = dataclasses.replace(trail.entries()[2], reason_code="tampered")

    intact, first_broken = trail.verify_chain_integrity()
    assert intact is False
    assert first_broken is not None


def test_an_untruncated_trail_behaves_as_before() -> None:
    trail = _trail(100)
    for i in range(10):
        trail.record("tick", "ok", {"i": i})

    assert trail.evicted_count() == 0
    assert len(trail) == trail.total_recorded() == 10
    assert trail.entries()[0].prev_hash == "0" * 64
    assert trail.verify_chain_integrity() == (True, None)
