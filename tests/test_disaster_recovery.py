"""Tests for the v8 disaster recovery reconciliation module."""

from __future__ import annotations

from src.diagnostics.disaster_recovery import (
    DiscrepancyType,
    PositionSnapshot,
    is_state_consistent,
    reconcile,
)


def test_matching_snapshots_no_discrepancies() -> None:
    local = [PositionSnapshot("BTC/USDT", 0.1)]
    reference = [PositionSnapshot("BTC/USDT", 0.1)]
    discrepancies = reconcile(local, reference)
    assert discrepancies == []
    assert is_state_consistent(discrepancies)


def test_missing_locally_detected() -> None:
    local: list[PositionSnapshot] = []
    reference = [PositionSnapshot("BTC/USDT", 0.1)]
    discrepancies = reconcile(local, reference)
    assert len(discrepancies) == 1
    assert discrepancies[0].discrepancy_type == DiscrepancyType.MISSING_LOCALLY


def test_missing_in_reference_detected() -> None:
    local = [PositionSnapshot("BTC/USDT", 0.1)]
    reference: list[PositionSnapshot] = []
    discrepancies = reconcile(local, reference)
    assert len(discrepancies) == 1
    assert discrepancies[0].discrepancy_type == DiscrepancyType.MISSING_IN_REFERENCE


def test_quantity_mismatch_detected() -> None:
    local = [PositionSnapshot("BTC/USDT", 0.1)]
    reference = [PositionSnapshot("BTC/USDT", 0.15)]
    discrepancies = reconcile(local, reference)
    assert len(discrepancies) == 1
    assert discrepancies[0].discrepancy_type == DiscrepancyType.QUANTITY_MISMATCH
    assert not is_state_consistent(discrepancies)


def test_tiny_difference_within_tolerance_ignored() -> None:
    local = [PositionSnapshot("BTC/USDT", 0.1)]
    reference = [PositionSnapshot("BTC/USDT", 0.1 + 1e-10)]
    discrepancies = reconcile(local, reference)
    assert discrepancies == []


def test_multiple_symbols_reconciled_independently() -> None:
    local = [PositionSnapshot("BTC/USDT", 0.1), PositionSnapshot("ETH/USDT", 1.0)]
    reference = [PositionSnapshot("BTC/USDT", 0.1), PositionSnapshot("ETH/USDT", 2.0)]
    discrepancies = reconcile(local, reference)
    assert len(discrepancies) == 1
    assert discrepancies[0].symbol == "ETH/USDT"


def test_empty_snapshots_consistent() -> None:
    discrepancies = reconcile([], [])
    assert is_state_consistent(discrepancies)
