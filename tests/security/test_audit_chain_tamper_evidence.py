"""
SECR-004 — the audit log is tamper-evident, and the tampering is what is tested.

An audit log's value is entirely in what it proves *after* an incident, which
means the interesting tests are the ones that modify it. Recording entries and
checking they verify proves the happy path and nothing about the guarantee.
So each test here takes an intact chain and breaks it in one specific way --
edit a field, edit a hash, delete an entry, reorder two, splice one in -- and
requires the verifier to notice, and to name the first sequence that failed.

The one case where the answer is subtle is eviction. This trail is bounded in
memory, so on a long-running process the genesis anchor is gone and
verification necessarily starts from the oldest retained entry. That is a
weaker claim than "the whole history is intact", and the tests below pin both
halves: the retained window is still fully verified, and the count of what is
no longer covered is exposed rather than hidden.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.diagnostics.audit_trail import AuditEntry, AuditTrail


@pytest.fixture
def trail() -> AuditTrail:
    t = AuditTrail()
    for i in range(10):
        t.record(
            event_type="order_placed",
            reason_code="signal",
            details={"symbol": "BTC/USDT", "qty": float(i)},
        )
    return t


def replace(trail: AuditTrail, index: int, **changes) -> None:
    """Overwrite one retained entry in place, as a tamperer with disk access would."""
    entries = list(trail._entries)
    entries[index] = dataclasses.replace(entries[index], **changes)
    trail._entries.clear()
    trail._entries.extend(entries)


class TestAnIntactChain:
    def test_it_verifies(self, trail):
        intact, broken = trail.verify_chain_integrity()
        assert intact is True
        assert broken is None

    def test_each_entry_links_to_its_predecessor(self, trail):
        entries = trail.entries()
        for prev, cur in zip(entries, entries[1:], strict=False):
            assert cur.prev_hash == prev.entry_hash

    def test_sequence_numbers_are_dense_and_increasing(self, trail):
        seqs = [e.sequence for e in trail.entries()]
        assert seqs == sorted(seqs)
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))

    def test_an_empty_trail_verifies(self):
        assert AuditTrail().verify_chain_integrity() == (True, None)


class TestModification:
    def test_editing_a_detail_is_detected(self, trail):
        # The realistic tampering: change what an order *said* it did.
        replace(trail, 4, details={"symbol": "BTC/USDT", "qty": 999.0})
        intact, broken = trail.verify_chain_integrity()
        assert intact is False
        assert broken == trail.entries()[4].sequence

    def test_editing_the_event_type_is_detected(self, trail):
        replace(trail, 2, event_type="order_cancelled")
        assert trail.verify_chain_integrity()[0] is False

    def test_editing_the_reason_code_is_detected(self, trail):
        replace(trail, 7, reason_code="operator_override")
        assert trail.verify_chain_integrity()[0] is False

    def test_editing_the_timestamp_is_detected(self, trail):
        # Moving an event in time is how a trade is made to look like it
        # preceded the decision that justified it.
        replace(trail, 3, ts_ms=1)
        assert trail.verify_chain_integrity()[0] is False

    def test_rewriting_the_stored_hash_to_match_is_still_detected(self, trail):
        # The obvious next move by a tamperer: edit the field, then recompute
        # the entry's own hash. The link to the *next* entry is what fails.
        from src.diagnostics.audit_trail import _compute_entry_hash

        entries = list(trail._entries)
        victim = entries[4]
        details = {"symbol": "BTC/USDT", "qty": 999.0}
        forged = _compute_entry_hash(
            victim.sequence,
            victim.ts_ms,
            victim.event_type,
            victim.reason_code,
            details,
            victim.prev_hash,
        )
        replace(trail, 4, details=details, entry_hash=forged)
        intact, broken = trail.verify_chain_integrity()
        assert intact is False
        # Reported at the *following* entry, whose prev_hash no longer matches.
        assert broken == trail.entries()[5].sequence

    def test_the_first_break_is_the_one_reported(self, trail):
        replace(trail, 2, reason_code="x")
        replace(trail, 6, reason_code="y")
        _, broken = trail.verify_chain_integrity()
        assert broken == trail.entries()[2].sequence


class TestDeletionAndReordering:
    def test_deleting_an_entry_is_detected(self, trail):
        entries = [e for i, e in enumerate(trail.entries()) if i != 5]
        trail._entries.clear()
        trail._entries.extend(entries)
        assert trail.verify_chain_integrity()[0] is False

    def test_deleting_the_last_entry_is_not_detectable_from_the_chain_alone(self, trail):
        # Stated rather than hidden: truncation at the tip leaves a chain that
        # is internally consistent. Detecting it needs an external anchor --
        # the recorded total, which is why total_recorded() exists and is
        # asserted here.
        before = trail.total_recorded()
        entries = list(trail.entries())[:-1]
        trail._entries.clear()
        trail._entries.extend(entries)
        assert trail.verify_chain_integrity()[0] is True
        assert trail.total_recorded() == before
        assert len(trail.entries()) < before

    def test_swapping_two_entries_is_detected(self, trail):
        entries = list(trail.entries())
        entries[3], entries[4] = entries[4], entries[3]
        trail._entries.clear()
        trail._entries.extend(entries)
        assert trail.verify_chain_integrity()[0] is False

    def test_splicing_in_a_forged_entry_is_detected(self, trail):
        entries = list(trail.entries())
        forged = AuditEntry(
            sequence=entries[5].sequence,
            ts_ms=entries[5].ts_ms,
            event_type="order_placed",
            reason_code="signal",
            details={"symbol": "BTC/USDT", "qty": -1.0},
            prev_hash=entries[4].entry_hash,
            entry_hash="f" * 64,
        )
        entries.insert(5, forged)
        trail._entries.clear()
        trail._entries.extend(entries)
        assert trail.verify_chain_integrity()[0] is False


class TestWhatEvictionCosts:
    def test_a_bounded_trail_still_verifies_what_it_holds(self):
        import src.diagnostics.audit_trail as module

        original = module._MAX_RETAINED_ENTRIES
        try:
            module._MAX_RETAINED_ENTRIES = 5
            trail = AuditTrail()
            # The deque's maxlen is captured at construction, so build it
            # after the bound is lowered.
            for i in range(20):
                trail.record("order_placed", "signal", {"i": i})
            intact, broken = trail.verify_chain_integrity()
            assert intact is True and broken is None
        finally:
            module._MAX_RETAINED_ENTRIES = original

    def test_eviction_does_not_renumber(self):
        import src.diagnostics.audit_trail as module

        original = module._MAX_RETAINED_ENTRIES
        try:
            module._MAX_RETAINED_ENTRIES = 5
            trail = AuditTrail()
            for i in range(20):
                trail.record("order_placed", "signal", {"i": i})
            seqs = [e.sequence for e in trail.entries()]
            # The retained window keeps its original numbering: restarting at
            # zero would make an evicted entry and a retained one collide.
            assert seqs[0] > 0
            assert trail.total_recorded() == 20
        finally:
            module._MAX_RETAINED_ENTRIES = original

    def test_the_uncovered_portion_is_reported_not_hidden(self):
        import src.diagnostics.audit_trail as module

        original = module._MAX_RETAINED_ENTRIES
        try:
            module._MAX_RETAINED_ENTRIES = 5
            trail = AuditTrail()
            for i in range(20):
                trail.record("order_placed", "signal", {"i": i})
            # "The chain is intact" and "the part I still hold is intact" are
            # different claims, and a consumer must be able to tell them apart.
            assert trail.evicted_count() == 15
        finally:
            module._MAX_RETAINED_ENTRIES = original

    def test_an_unevicted_trail_reports_zero(self, trail):
        assert trail.evicted_count() == 0
