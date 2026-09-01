"""Tests for the group/global notional budget behind /risk/size-check.

`get_budget` was referenced by src/api/main.py but had never been written, so
every call to the endpoint raised ImportError and returned 500. These cover
the checker that now backs it.
"""

from __future__ import annotations

import pytest

from src.execution.unified_ledger import UnifiedLedger, VenuePosition
from src.risk.macro_exposure_budget import (
    MacroExposureBudgetChecker,
    get_budget,
    group_for_symbol,
)


@pytest.fixture
def ledger(monkeypatch):
    book = UnifiedLedger()
    monkeypatch.setattr("src.execution.unified_ledger.get_unified_ledger", lambda: book)
    return book


def test_known_symbols_map_to_their_group():
    assert group_for_symbol("btc/usdt") == "crypto_large_cap"
    assert group_for_symbol("SOL/USDT") == "crypto_mid_cap"


def test_unknown_symbols_share_the_other_group():
    assert group_for_symbol("DOGE/USDT") == "other"


def test_capital_must_be_positive():
    with pytest.raises(ValueError, match="capital_usd"):
        MacroExposureBudgetChecker(capital_usd=0.0)


def test_an_empty_book_allows_a_notional_under_the_group_ceiling(ledger):
    check = get_budget(100_000.0).check(
        symbol="BTC/USDT", group="crypto_large_cap", requested_notional=10_000.0
    )

    assert check.allowed is True
    assert check.reason == ""
    assert check.current_group_notional == 0.0
    assert check.group_limit == pytest.approx(40_000.0)


def test_a_non_positive_request_is_refused(ledger):
    check = get_budget(100_000.0).check(
        symbol="BTC/USDT", group="crypto_large_cap", requested_notional=0.0
    )

    assert check.allowed is False
    assert "non_positive_notional" in check.reason


def test_open_positions_in_the_group_count_against_the_group_ceiling(ledger):
    ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.7, 50_000.0, 3_500.0))

    check = get_budget(100_000.0).check(
        symbol="ETH/USDT", group="crypto_large_cap", requested_notional=10_000.0
    )

    assert check.current_group_notional == pytest.approx(35_000.0)
    assert check.allowed is False
    assert "group_limit_exceeded" in check.reason


def test_positions_in_other_groups_only_count_globally(ledger):
    ledger.record_position(VenuePosition("binance", "SOL/USDT", 1_000.0, 50.0, 5_000.0))

    check = get_budget(100_000.0).check(
        symbol="BTC/USDT", group="crypto_large_cap", requested_notional=10_000.0
    )

    assert check.current_group_notional == 0.0
    assert check.current_global_notional == pytest.approx(50_000.0)
    assert check.allowed is True


def test_the_whole_book_ceiling_refuses_what_the_group_ceiling_would_allow(ledger):
    # spread across groups so no single group is full, but the book is
    ledger.record_position(VenuePosition("binance", "SOL/USDT", 1_000.0, 60.0, 6_000.0))
    ledger.record_position(VenuePosition("okx", "DOGE/USDT", 1_000.0, 40.0, 4_000.0))

    check = get_budget(100_000.0).check(
        symbol="BTC/USDT", group="crypto_large_cap", requested_notional=1_000.0
    )

    assert check.current_global_notional == pytest.approx(100_000.0)
    assert check.allowed is False
    assert "global_limit_exceeded" in check.reason
