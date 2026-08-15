"""Tests for the v8 immutable, hash-chained audit trail."""

from __future__ import annotations

from src.diagnostics.audit_trail import AuditTrail, get_audit_trail


def test_record_returns_entry_with_sequence_zero_first() -> None:
    trail = AuditTrail()
    entry = trail.record("order_placed", "signal_tradeable")
    assert entry.sequence == 0
    assert entry.prev_hash == "0" * 64


def test_sequence_increments() -> None:
    trail = AuditTrail()
    trail.record("order_placed", "signal_tradeable")
    e2 = trail.record("order_filled", "fill_confirmed")
    assert e2.sequence == 1
    assert e2.prev_hash == trail.entries()[0].entry_hash


def test_chain_integrity_holds_for_unmodified_trail() -> None:
    trail = AuditTrail()
    for i in range(5):
        trail.record("risk_gate_fired", f"reason_{i}", {"i": i})
    intact, broken = trail.verify_chain_integrity()
    assert intact
    assert broken is None


def test_tampering_detected() -> None:
    trail = AuditTrail()
    trail.record("order_placed", "signal_tradeable")
    trail.record("order_filled", "fill_confirmed")

    tampered_entries = list(trail.entries())
    from dataclasses import replace

    tampered_entries[0] = replace(tampered_entries[0], reason_code="tampered")
    trail._entries[0] = tampered_entries[0]  # type: ignore[attr-defined]

    intact, broken = trail.verify_chain_integrity()
    assert not intact
    assert broken == 0


def test_empty_trail_is_intact() -> None:
    trail = AuditTrail()
    intact, broken = trail.verify_chain_integrity()
    assert intact
    assert broken is None


def test_len_reflects_entry_count() -> None:
    trail = AuditTrail()
    trail.record("a", "b")
    trail.record("c", "d")
    assert len(trail) == 2


def test_get_audit_trail_singleton() -> None:
    t1 = get_audit_trail()
    t2 = get_audit_trail()
    assert t1 is t2


def test_explicit_ts_ms_used_when_provided() -> None:
    trail = AuditTrail()
    entry = trail.record("a", "b", ts_ms=12345)
    assert entry.ts_ms == 12345


def test_details_default_to_empty_dict() -> None:
    trail = AuditTrail()
    entry = trail.record("a", "b")
    assert entry.details == {}
