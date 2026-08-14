"""
Wiring tests for the v5 Greeks exposure ceilings.

greeks.py had Black-Scholes Greeks and a cap check with no caller: the
options-carry strategy emitted premium-selling signals with entirely
unmeasured delta and vega. Notional-based Kelly sizing cannot see that
exposure, which is the reason the module exists separately.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.risk.greeks import GreeksExposureCaps
from src.strategies.options_carry import (
    OptionsCarryContext,
    OptionsCarryStrategy,
    _caps_from_config,
)


def _rich_vol_context(**overrides) -> OptionsCarryContext:
    """IV rich enough to trade, with an at-the-money 30-day call."""
    base = {
        "implied_vol_zscore": 2.0,
        "holding_direction": 1,
        "spot": 100.0,
        "strike": 100.0,
        "time_to_expiry_years": 30.0 / 365.0,
        "implied_vol": 0.60,
        "is_call": True,
        "contracts": 1.0,
    }
    base.update(overrides)
    return OptionsCarryContext(**base)


class TestNoCapsConfigured:
    def test_signal_is_unchanged_when_no_caps_are_set(self) -> None:
        """The behaviour that existed before the setting did."""
        strategy = OptionsCarryStrategy(greeks_caps=None)
        signal = strategy.generate_signal(
            OptionsCarryContext(implied_vol_zscore=2.0, holding_direction=1)
        )
        assert signal.direction == 1

    def test_vol_not_rich_enough_is_still_flat(self) -> None:
        strategy = OptionsCarryStrategy(greeks_caps=None)
        signal = strategy.generate_signal(
            OptionsCarryContext(implied_vol_zscore=0.2, holding_direction=1)
        )
        assert signal.direction == 0


class TestCapsEnforced:
    _GENEROUS = GreeksExposureCaps(max_abs_delta=100.0, max_abs_vega=100.0)
    _TIGHT = GreeksExposureCaps(max_abs_delta=0.01, max_abs_vega=100.0)

    def test_within_caps_still_trades(self) -> None:
        strategy = OptionsCarryStrategy(greeks_caps=self._GENEROUS)
        assert strategy.generate_signal(_rich_vol_context()).direction == 1

    def test_delta_breach_vetoes_the_signal(self) -> None:
        strategy = OptionsCarryStrategy(greeks_caps=self._TIGHT)
        signal = strategy.generate_signal(_rich_vol_context())
        assert signal.direction == 0
        assert signal.confidence == 0.0

    def test_vega_breach_vetoes_the_signal(self) -> None:
        strategy = OptionsCarryStrategy(
            greeks_caps=GreeksExposureCaps(max_abs_delta=100.0, max_abs_vega=0.001)
        )
        assert strategy.generate_signal(_rich_vol_context()).direction == 0

    def test_existing_book_exposure_counts_toward_the_cap(self) -> None:
        """The ceiling is portfolio-level, not per-trade."""
        strategy = OptionsCarryStrategy(
            greeks_caps=GreeksExposureCaps(max_abs_delta=1.0, max_abs_vega=100.0)
        )
        assert strategy.generate_signal(_rich_vol_context()).direction == 1
        crowded = _rich_vol_context(portfolio_delta=-0.9)
        assert strategy.generate_signal(crowded).direction == 0

    def test_more_contracts_scale_the_exposure(self) -> None:
        strategy = OptionsCarryStrategy(
            greeks_caps=GreeksExposureCaps(max_abs_delta=1.0, max_abs_vega=100.0)
        )
        assert strategy.generate_signal(_rich_vol_context(contracts=1.0)).direction == 1
        assert strategy.generate_signal(_rich_vol_context(contracts=50.0)).direction == 0

    def test_selling_premium_carries_the_negative_of_long_greeks(self) -> None:
        """
        A short call is short delta. An existing long book therefore nets
        DOWN against it, and the same contract that breaches on its own
        passes once the book offsets it.
        """
        caps = GreeksExposureCaps(max_abs_delta=0.6, max_abs_vega=100.0)
        strategy = OptionsCarryStrategy(greeks_caps=caps)
        # Short ATM call delta is about -0.55; alone that is within 0.6.
        assert strategy.generate_signal(_rich_vol_context()).direction == 1
        # A long book pushes the net the other way, back toward zero.
        assert strategy.generate_signal(_rich_vol_context(portfolio_delta=0.5)).direction == 1
        # A short book compounds it and breaches.
        assert strategy.generate_signal(_rich_vol_context(portfolio_delta=-0.5)).direction == 0


class TestUnmeasurableContracts:
    _CAPS = GreeksExposureCaps(max_abs_delta=100.0, max_abs_vega=100.0)

    def test_missing_contract_terms_veto_when_caps_are_configured(self) -> None:
        """
        Once an operator asks for a ceiling, waving through a position whose
        Greeks cannot be measured defeats the point of asking.
        """
        strategy = OptionsCarryStrategy(greeks_caps=self._CAPS)
        signal = strategy.generate_signal(
            OptionsCarryContext(implied_vol_zscore=2.0, holding_direction=1)
        )
        assert signal.direction == 0

    @pytest.mark.parametrize("field", ["spot", "strike", "time_to_expiry_years", "implied_vol"])
    def test_any_single_missing_term_vetoes(self, field: str) -> None:
        strategy = OptionsCarryStrategy(greeks_caps=self._CAPS)
        assert strategy.generate_signal(_rich_vol_context(**{field: None})).direction == 0

    @pytest.mark.parametrize(
        ("field", "value"),
        [("spot", 0.0), ("strike", -1.0), ("time_to_expiry_years", 0.0), ("implied_vol", 0.0)],
    )
    def test_unusable_terms_veto_rather_than_raise(self, field: str, value: float) -> None:
        """compute_greeks raises ValueError on these; a strategy must not."""
        strategy = OptionsCarryStrategy(greeks_caps=self._CAPS)
        assert strategy.generate_signal(_rich_vol_context(**{field: value})).direction == 0


class TestCapsFromConfig:
    def _cfg(self, delta, vega):
        from src.config import StrategyPortfolioSettings

        return StrategyPortfolioSettings(
            options_carry_max_abs_delta=delta,
            options_carry_max_abs_vega=vega,
        )

    def test_neither_configured_gives_no_caps(self) -> None:
        assert _caps_from_config(self._cfg(None, None)) is None

    def test_both_configured(self) -> None:
        caps = _caps_from_config(self._cfg(2.0, 3.0))
        assert caps == GreeksExposureCaps(max_abs_delta=2.0, max_abs_vega=3.0)

    def test_half_configuration_is_rejected_before_it_reaches_here(self) -> None:
        """
        A one-sided cap is refused at settings construction, so
        _caps_from_config never has to decide what half a ceiling means.
        """
        with pytest.raises(ValidationError):
            self._cfg(2.0, None)
        with pytest.raises(ValidationError):
            self._cfg(None, 3.0)

    def test_strategy_resolves_caps_from_config_when_not_injected(self) -> None:
        strategy = OptionsCarryStrategy(cfg=self._cfg(0.01, 100.0))
        assert strategy.generate_signal(_rich_vol_context()).direction == 0
