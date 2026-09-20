"""
DATA-004 — money arithmetic uses a declared, exact-where-it-matters
representation.

The policy is written down in `docs/quality/MONEY_AND_TIME.md` and it is not
the flattering one: **this project uses float64 for money, not Decimal.**
Saying so is more useful than an aspirational claim the tree does not support,
and it turns the requirement into something checkable — because a declared
float policy has obligations that an undeclared one does not:

1. `Decimal` is mandatory at the exchange-precision boundary, where the
   binary representation error is visible at eight decimal places.
2. Quantising must never round *up*. Rounding up can push an order past a
   limit that was checked before quantisation, and past the venue's balance.
3. Accumulated drift over a trading day's fills must stay inside the declared
   1e-9 relative tolerance — six orders of magnitude inside float64's own
   precision, so a breach is a logic error rather than rounding.
4. Percent and fraction are different units wearing the same `_pct` suffix,
   and the difference has to be pinned rather than remembered.

Each is a test below.
"""

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path

import pytest

from src.config import RiskSettings
from src.execution.unified_ledger import UnifiedLedger, VenuePosition
from src.risk.kelly import _floor_to_precision

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: The declared tolerance from docs/quality/MONEY_AND_TIME.md §4.
DECLARED_RELATIVE_TOLERANCE = 1e-9


class TestTheBoundaryUsesDecimal:
    def test_quantisation_matches_the_human_readable_value(self):
        # The regression this closes: math.floor(v * 10**n) / 10**n has
        # visible representation artifacts at eight decimal places on BTC
        # quantities. Decimal(str(v)) round-trips through the string.
        assert _floor_to_precision(0.123456789, 6) == 0.123456
        assert _floor_to_precision(0.1, 8) == 0.1

    @pytest.mark.parametrize(
        ("value", "places", "expected"),
        [
            (1.005, 2, 1.0),
            (0.000000019, 8, 0.00000001),
            (12345.6789, 0, 12345.0),
            (0.0, 8, 0.0),
            (99.999999999, 8, 99.99999999),
        ],
    )
    def test_it_always_floors(self, value, places, expected):
        assert _floor_to_precision(value, places) == pytest.approx(expected, abs=1e-12)

    @pytest.mark.parametrize("value", [0.1, 1.0, 3.3, 12345.678, 0.00000001])
    @pytest.mark.parametrize("places", [0, 2, 6, 8])
    def test_quantising_never_increases_a_quantity(self, value, places):
        # The property that matters operationally: a quantity checked against
        # a limit before quantisation must still be inside it afterwards.
        assert _floor_to_precision(value, places) <= value + 1e-12

    def test_quantising_is_idempotent(self):
        once = _floor_to_precision(0.123456789, 6)
        assert _floor_to_precision(once, 6) == once

    def test_a_negative_precision_is_refused(self):
        with pytest.raises(ValueError, match="decimal_places"):
            _floor_to_precision(1.0, -1)

    def test_the_string_round_trip_is_what_makes_it_exact(self):
        # Stated as an assertion rather than a comment: the direct binary
        # conversion carries the representation error into the decimal, and
        # the string conversion does not.
        assert Decimal(str(0.1)) == Decimal("0.1")
        assert Decimal(0.1) != Decimal("0.1")


class TestAccumulatedDriftStaysInsideTheDeclaredTolerance:
    def test_a_trading_days_fills_agree_with_an_exact_reference(self):
        """
        Accumulate a day's worth of fills both ways and compare.

        A single arithmetic expression cannot fail this way, which is why the
        check runs over a long sequence: float drift is a property of the
        chain, not of any one operation.
        """
        ledger = UnifiedLedger()
        exact_qty = Decimal(0)
        exact_margin = Decimal(0)

        # 1,440 one-minute fills: a full day on the fastest timeframe.
        for i in range(1_440):
            qty_s = f"0.{i % 97:02d}345678"
            margin_s = f"{(i % 913) + 1}.37"
            ledger.record_position(
                VenuePosition(
                    venue=f"venue_{i}",
                    symbol="BTC/USDT",
                    quantity=float(qty_s),
                    entry_price=50_000.0 + i,
                    margin_used_usd=float(margin_s),
                )
            )
            exact_qty += Decimal(qty_s)
            exact_margin += Decimal(margin_s)

        assert ledger.net_exposure("BTC/USDT") == pytest.approx(
            float(exact_qty), rel=DECLARED_RELATIVE_TOLERANCE
        )
        assert ledger.total_margin_used_usd() == pytest.approx(
            float(exact_margin), rel=DECLARED_RELATIVE_TOLERANCE
        )

    def test_longs_and_shorts_net_rather_than_cancelling_to_noise(self):
        ledger = UnifiedLedger()
        for i in range(500):
            ledger.record_position(VenuePosition(f"long_{i}", "BTC/USDT", 0.1, 50_000.0, 100.0))
            ledger.record_position(VenuePosition(f"short_{i}", "BTC/USDT", -0.1, 50_000.0, 100.0))
        # Exactly flat, and gross exposure still records that 1,000 positions
        # are open -- netting to zero must not read as "no exposure".
        assert ledger.net_exposure("BTC/USDT") == pytest.approx(0.0, abs=1e-9)
        assert ledger.gross_exposure("BTC/USDT") == pytest.approx(100.0, rel=1e-12)

    def test_margin_scopes_to_one_venue(self):
        ledger = UnifiedLedger()
        ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.1, 60_000.0, 600.0))
        ledger.record_position(VenuePosition("okx", "BTC/USDT", -0.05, 60_100.0, 300.0))
        assert ledger.total_margin_used_usd() == pytest.approx(900.0)
        assert ledger.total_margin_used_usd("binance") == pytest.approx(600.0)

    def test_one_position_per_venue_and_symbol(self):
        # Two accumulators for one quantity drift independently and then
        # disagree for reasons nobody can reconstruct. Upsert, do not append.
        ledger = UnifiedLedger()
        ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.1, 60_000.0, 600.0))
        ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.2, 60_000.0, 1_200.0))
        assert ledger.net_exposure("BTC/USDT") == pytest.approx(0.2)
        assert len(ledger.positions_for_symbol("BTC/USDT")) == 1

    def test_a_cleared_position_leaves_no_residue(self):
        ledger = UnifiedLedger()
        ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.1, 60_000.0, 600.0))
        ledger.clear_position("binance", "BTC/USDT")
        assert ledger.net_exposure("BTC/USDT") == 0.0
        assert ledger.total_margin_used_usd() == 0.0
        assert ledger.venues_holding("BTC/USDT") == []

    def test_clearing_an_absent_position_is_harmless(self):
        UnifiedLedger().clear_position("binance", "BTC/USDT")


class TestPercentAndFractionAreDifferentUnits:
    """
    The two `_pct` fields that mean different things.

    `daily_drawdown_halt_pct` is a percent (2.0 means 2%);
    `capital_preservation_max_drawdown_pct` is a fraction (0.30 means 30%).
    Both names are `RISK_*` environment variable names, so they cannot be
    renamed out from under a deployment -- which makes pinning the units the
    only defence.
    """

    @pytest.fixture
    def cfg(self) -> RiskSettings:
        return RiskSettings(_env_file=None)

    def test_the_daily_drawdown_limit_is_a_percent(self, cfg):
        assert cfg.daily_drawdown_halt_pct == 2.0
        assert 0.1 <= cfg.daily_drawdown_halt_pct <= 10.0

    def test_the_capital_preservation_floor_is_a_fraction(self, cfg):
        assert cfg.capital_preservation_max_drawdown_pct == 0.30
        assert 0.0 < cfg.capital_preservation_max_drawdown_pct < 1.0

    def test_a_percent_shaped_value_is_refused_by_the_fraction_field(self):
        # 30.0 would mean "3000%" if the units were swapped. The validator is
        # what stops a plausible-looking edit from disabling the floor.
        with pytest.raises(ValueError):
            RiskSettings(_env_file=None, capital_preservation_max_drawdown_pct=30.0)

    def test_a_fraction_shaped_value_is_refused_by_the_percent_field(self):
        # 0.02 would mean "0.02%", a floor that halts on the first tick.
        with pytest.raises(ValueError):
            RiskSettings(_env_file=None, daily_drawdown_halt_pct=0.02)

    def test_the_position_size_limit_is_a_percent(self, cfg):
        assert cfg.max_position_size_pct == 5.0
        assert 0.1 <= cfg.max_position_size_pct <= 25.0


@pytest.fixture(scope="module")
def document() -> str:
    return (PROJECT_ROOT / "docs" / "quality" / "MONEY_AND_TIME.md").read_text(encoding="utf-8")


class TestThePolicyIsWrittenDown:
    """A declared representation that nobody wrote down is not declared."""

    def test_it_states_the_representation_plainly(self, document):
        assert "float64` for money, not `Decimal`" in document

    def test_it_names_the_decimal_boundary(self, document):
        assert "_floor_to_precision" in document
        assert "ROUND_DOWN" in document

    def test_it_states_the_tolerance_this_suite_asserts(self, document):
        assert "1e-9" in document

    def test_it_records_when_the_decision_should_be_revisited(self, document):
        assert "When to revisit" in document


class TestNoSilentPrecisionLoss:
    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_quantising_a_non_finite_quantity_is_refused(self, bad):
        # Found by writing this test. `Decimal(str(nan))` is `Decimal('NaN')`
        # and quantizing it *returns NaN* rather than raising, so a NaN
        # quantity came out the other side still NaN -- wearing the authority
        # of a value that had been "quantised to exchange precision". An
        # infinity raised InvalidOperation, so the two cases behaved
        # differently for no reason a caller could act on.
        with pytest.raises(ValueError, match="non-finite quantity"):
            _floor_to_precision(bad, 8)
