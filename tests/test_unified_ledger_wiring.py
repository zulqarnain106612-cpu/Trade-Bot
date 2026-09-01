"""
Wiring tests for the v3 unified cross-venue ledger.

The ledger had accounting logic and no producer, so the correlation ceiling
was computed against a single executor's book. These cover the orchestrator
aggregation/sync and the read path that now feeds it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from conftest import settings_double

from src.config import Timeframe, TradingMode
from src.execution.unified_ledger import UnifiedLedger, VenuePosition


def _pos(symbol: str, direction: str, quantity: float, price: float, notional: float):
    return {
        "symbol": symbol,
        "direction": direction,
        "quantity": quantity,
        "entry_price": price,
        "notional_usd": notional,
    }


def _make_orch(symbol: str = "BTC/USDT"):
    from src.engine.orchestrator import Orchestrator

    with patch("src.engine.orchestrator.get_settings") as mock_cfg:
        cfg = settings_double()
        cfg.primary_symbol = symbol
        cfg.active_timeframes = [Timeframe.INTRADAY]
        cfg.primary_timeframe = Timeframe.INTRADAY
        cfg.trading_mode = TradingMode.PAPER
        cfg.starting_capital_usd = 1000.0
        cfg.storage.model_dir = "/tmp/models"
        mock_cfg.return_value = cfg
        return Orchestrator(MagicMock(), MagicMock())


@pytest.fixture
def ledger():
    """A fresh ledger per test — the real one is a process-wide singleton."""
    fresh = UnifiedLedger()
    with patch("src.engine.orchestrator.get_unified_ledger", return_value=fresh):
        yield fresh


class TestAggregation:
    def _aggregate(self, positions):
        from src.engine.orchestrator import _aggregate_venue_positions

        return {p.symbol: p for p in _aggregate_venue_positions("binance", positions)}

    def test_same_symbol_across_timeframes_is_netted_not_overwritten(self) -> None:
        """
        UnifiedLedger keys on (venue, symbol), but one executor holds a
        position per timeframe — recording them raw would silently drop all
        but the last.
        """
        result = self._aggregate(
            [
                _pos("BTC/USDT", "long", 0.2, 60_000.0, 12_000.0),
                _pos("BTC/USDT", "long", 0.1, 62_000.0, 6_200.0),
            ]
        )
        assert result["BTC/USDT"].quantity == pytest.approx(0.3)
        assert result["BTC/USDT"].margin_used_usd == pytest.approx(18_200.0)

    def test_shorts_carry_a_negative_quantity(self) -> None:
        result = self._aggregate([_pos("ETH/USDT", "short", 2.0, 3_000.0, 6_000.0)])
        assert result["ETH/USDT"].quantity == pytest.approx(-2.0)

    def test_opposing_legs_net_to_zero_but_still_commit_margin(self) -> None:
        result = self._aggregate(
            [
                _pos("BTC/USDT", "long", 0.1, 60_000.0, 6_000.0),
                _pos("BTC/USDT", "short", 0.1, 61_000.0, 6_100.0),
            ]
        )
        assert result["BTC/USDT"].quantity == pytest.approx(0.0)
        assert result["BTC/USDT"].margin_used_usd == pytest.approx(12_100.0)

    def test_entry_price_is_gross_quantity_weighted(self) -> None:
        result = self._aggregate(
            [
                _pos("BTC/USDT", "long", 3.0, 100.0, 300.0),
                _pos("BTC/USDT", "long", 1.0, 200.0, 200.0),
            ]
        )
        assert result["BTC/USDT"].entry_price == pytest.approx(125.0)

    def test_zero_quantity_and_unnamed_rows_are_skipped(self) -> None:
        result = self._aggregate(
            [
                _pos("BTC/USDT", "long", 0.0, 60_000.0, 0.0),
                _pos("", "long", 1.0, 1.0, 1.0),
            ]
        )
        assert result == {}

    def test_missing_fields_do_not_raise(self) -> None:
        result = self._aggregate([{"symbol": "BTC/USDT", "quantity": 1.0}])
        assert result["BTC/USDT"].entry_price == pytest.approx(0.0)
        # No direction key => not "long" => treated as short, i.e. the
        # conservative reading rather than an assumed long.
        assert result["BTC/USDT"].quantity == pytest.approx(-1.0)


class TestSyncAndRead:
    def test_publishes_this_venues_book(self, ledger: UnifiedLedger) -> None:
        orch = _make_orch()
        orch._sync_and_read_ledger("binance", [_pos("ETH/USDT", "long", 1.0, 3_000.0, 3_000.0)])
        assert ledger.net_exposure("ETH/USDT") == pytest.approx(1.0)

    def test_closed_positions_disappear_from_the_venue(self, ledger: UnifiedLedger) -> None:
        """An incremental update would leave a closed position on the book."""
        orch = _make_orch()
        orch._sync_and_read_ledger("binance", [_pos("ETH/USDT", "long", 1.0, 3_000.0, 3_000.0)])
        orch._sync_and_read_ledger("binance", [])
        assert ledger.all_positions == []

    def test_other_venues_are_not_disturbed(self, ledger: UnifiedLedger) -> None:
        ledger.record_position(VenuePosition("okx", "SOL/USDT", 5.0, 150.0, 750.0))
        orch = _make_orch()
        orch._sync_and_read_ledger("binance", [])
        assert ledger.venues_holding("SOL/USDT") == ["okx"]

    def test_own_symbol_is_excluded_from_the_correlation_input(self, ledger: UnifiedLedger) -> None:
        orch = _make_orch("BTC/USDT")
        others = orch._sync_and_read_ledger(
            "binance",
            [
                _pos("BTC/USDT", "long", 0.1, 60_000.0, 6_000.0),
                _pos("ETH/USDT", "long", 1.0, 3_000.0, 3_000.0),
            ],
        )
        assert others == ["ETH/USDT"]

    def test_reads_exposure_held_by_another_orchestrators_venue(
        self, ledger: UnifiedLedger
    ) -> None:
        """
        The whole point of the ledger: an executor-only view could not see a
        position opened by a different symbol's orchestrator, which biased
        the correlation ceiling toward "uncorrelated" — i.e. toward sizing up.
        """
        ledger.record_position(VenuePosition("okx", "SOL/USDT", 5.0, 150.0, 750.0))
        orch = _make_orch("BTC/USDT")
        others = orch._sync_and_read_ledger("binance", [])
        assert others == ["SOL/USDT"]

    def test_hedged_symbol_still_counts_as_exposure(self, ledger: UnifiedLedger) -> None:
        """Gross, not net: a netted-out symbol still carries correlation risk."""
        ledger.record_position(VenuePosition("okx", "ETH/USDT", -1.0, 3_000.0, 3_000.0))
        orch = _make_orch("BTC/USDT")
        others = orch._sync_and_read_ledger(
            "binance", [_pos("ETH/USDT", "long", 1.0, 3_000.0, 3_000.0)]
        )
        assert others == ["ETH/USDT"]

    def test_ledger_fault_falls_back_to_the_executor_view(self) -> None:
        """A ledger bug must not remove the correlation ceiling entirely."""
        orch = _make_orch("BTC/USDT")
        with patch(
            "src.engine.orchestrator.get_unified_ledger",
            side_effect=RuntimeError("ledger down"),
        ):
            others = orch._sync_and_read_ledger(
                "binance",
                [
                    _pos("BTC/USDT", "long", 0.1, 60_000.0, 6_000.0),
                    _pos("ETH/USDT", "long", 1.0, 3_000.0, 3_000.0),
                ],
            )
        assert others == ["ETH/USDT"]


class TestVenueSelection:
    def test_paper_executor_gets_its_own_venue(self) -> None:
        """Paper fills are not exchange exposure and must not net against it."""
        from src.execution.paper import PaperExecutor

        orch = _make_orch()
        assert orch._venue_for(MagicMock(spec=PaperExecutor)) == "paper"

    def test_live_executor_maps_to_the_exchange(self) -> None:
        from src.config import EXCHANGE_BINANCE
        from src.execution.live import LiveExecutor

        orch = _make_orch()
        assert orch._venue_for(MagicMock(spec=LiveExecutor)) == EXCHANGE_BINANCE
