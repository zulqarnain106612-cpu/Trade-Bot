"""Tests for the v3 unified cross-exchange ledger."""

from __future__ import annotations

import pytest

from src.execution.unified_ledger import UnifiedLedger, VenuePosition, get_unified_ledger


def test_record_and_net_exposure() -> None:
    ledger = UnifiedLedger()
    ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.1, 60000.0, 600.0))
    ledger.record_position(VenuePosition("okx", "BTC/USDT", -0.05, 60100.0, 300.0))
    assert ledger.net_exposure("BTC/USDT") == 0.05


def test_gross_exposure_sums_absolute_values() -> None:
    ledger = UnifiedLedger()
    ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.1, 60000.0, 600.0))
    ledger.record_position(VenuePosition("okx", "BTC/USDT", -0.05, 60100.0, 300.0))
    assert ledger.gross_exposure("BTC/USDT") == pytest.approx(0.15)


def test_upsert_replaces_same_venue_symbol() -> None:
    ledger = UnifiedLedger()
    ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.1, 60000.0, 600.0))
    ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.2, 61000.0, 1200.0))
    assert ledger.net_exposure("BTC/USDT") == 0.2
    assert len(ledger.positions_for_symbol("BTC/USDT")) == 1


def test_clear_position_removes_entry() -> None:
    ledger = UnifiedLedger()
    ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.1, 60000.0, 600.0))
    ledger.clear_position("binance", "BTC/USDT")
    assert ledger.net_exposure("BTC/USDT") == 0.0
    assert ledger.positions_for_symbol("BTC/USDT") == []


def test_total_margin_used_scoped_and_global() -> None:
    ledger = UnifiedLedger()
    ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.1, 60000.0, 600.0))
    ledger.record_position(VenuePosition("okx", "ETH/USDT", 1.0, 3000.0, 300.0))
    assert ledger.total_margin_used_usd() == 900.0
    assert ledger.total_margin_used_usd("binance") == 600.0


def test_venues_holding_returns_venue_list() -> None:
    ledger = UnifiedLedger()
    ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.1, 60000.0, 600.0))
    ledger.record_position(VenuePosition("okx", "BTC/USDT", -0.05, 60100.0, 300.0))
    assert set(ledger.venues_holding("BTC/USDT")) == {"binance", "okx"}


def test_all_positions_returns_everything() -> None:
    ledger = UnifiedLedger()
    ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.1, 60000.0, 600.0))
    assert len(ledger.all_positions) == 1


def test_get_unified_ledger_singleton() -> None:
    l1 = get_unified_ledger()
    l2 = get_unified_ledger()
    assert l1 is l2


def test_no_positions_returns_zero_exposure() -> None:
    ledger = UnifiedLedger()
    assert ledger.net_exposure("BTC/USDT") == 0.0
    assert ledger.gross_exposure("BTC/USDT") == 0.0
