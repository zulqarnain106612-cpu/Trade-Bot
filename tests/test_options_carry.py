"""Tests for the v5 options carry strategy."""

from __future__ import annotations

import pytest

from src.risk.greeks import GreeksExposureCaps
from src.strategies.options_carry import OptionsCarryContext, OptionsCarryStrategy
from src.strategies.registry import StrategyRegistry


def test_rejects_non_optionscarrycontext_bar() -> None:
    strat = OptionsCarryStrategy()
    with pytest.raises(TypeError, match="OptionsCarryContext"):
        strat.generate_signal(bar=None)


def test_flat_when_iv_not_stretched() -> None:
    strat = OptionsCarryStrategy()
    ctx = OptionsCarryContext(implied_vol_zscore=0.5, holding_direction=1)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_flat_when_no_holding_direction() -> None:
    strat = OptionsCarryStrategy()
    ctx = OptionsCarryContext(implied_vol_zscore=2.0, holding_direction=0)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_covered_call_signal_when_long_and_iv_rich() -> None:
    strat = OptionsCarryStrategy()
    ctx = OptionsCarryContext(implied_vol_zscore=2.0, holding_direction=1)
    sig = strat.generate_signal(ctx)
    assert sig.direction == 1
    assert sig.confidence > 0.0


def test_cash_secured_put_signal_when_flat_and_iv_rich() -> None:
    strat = OptionsCarryStrategy()
    ctx = OptionsCarryContext(implied_vol_zscore=2.0, holding_direction=-1)
    sig = strat.generate_signal(ctx)
    assert sig.direction == -1


def test_registers_with_registry() -> None:
    registry = StrategyRegistry()
    registry.register(OptionsCarryStrategy())
    assert "options_carry_v1" in registry


def test_rejects_invalid_capital_fraction() -> None:
    with pytest.raises(ValueError, match="max_capital_fraction"):
        OptionsCarryStrategy(max_capital_fraction=0.0)


# ---------------------------------------------------------------------------
# Greeks exposure gate
# ---------------------------------------------------------------------------


def _rich_iv_context(**overrides: float) -> OptionsCarryContext:
    """A context that is unambiguously actionable before the Greeks gate runs."""
    base: dict[str, float] = {
        "implied_vol_zscore": 3.0,
        "holding_direction": 1,
        "spot": 60_000.0,
        "strike": 66_000.0,
        "time_to_expiry_years": 0.25,
        "implied_vol": 0.6,
        "contracts": 1.0,
    }
    base.update(overrides)
    return OptionsCarryContext(**base)  # type: ignore[arg-type]


def test_no_caps_configured_leaves_signal_untouched() -> None:
    strat = OptionsCarryStrategy()
    assert strat.generate_signal(_rich_iv_context()).direction == 1


def test_signal_survives_when_projected_greeks_are_within_caps() -> None:
    strat = OptionsCarryStrategy(greeks_caps=GreeksExposureCaps(1e6, 1e6))
    assert strat.generate_signal(_rich_iv_context()).direction == 1


def test_delta_cap_breach_suppresses_the_signal() -> None:
    strat = OptionsCarryStrategy(greeks_caps=GreeksExposureCaps(0.01, 1e6))
    signal = strat.generate_signal(_rich_iv_context())
    assert signal.direction == 0
    assert signal.confidence == 0.0


def test_vega_cap_breach_suppresses_the_signal() -> None:
    strat = OptionsCarryStrategy(greeks_caps=GreeksExposureCaps(1e6, 0.01))
    assert strat.generate_signal(_rich_iv_context()).direction == 0


def test_existing_book_exposure_counts_toward_the_cap() -> None:
    """A contract that fits on an empty book must not fit on a loaded one."""
    caps = GreeksExposureCaps(max_abs_delta=0.5, max_abs_vega=1e6)
    strat = OptionsCarryStrategy(greeks_caps=caps)
    assert strat.generate_signal(_rich_iv_context()).direction == 1
    loaded = _rich_iv_context(portfolio_delta=-0.45)
    assert strat.generate_signal(loaded).direction == 0


def test_selling_calls_pushes_delta_negative() -> None:
    """Short call = negative delta, so a negative cap breach comes from below."""
    caps = GreeksExposureCaps(max_abs_delta=0.5, max_abs_vega=1e6)
    strat = OptionsCarryStrategy(greeks_caps=caps)
    # Positive existing delta is offset by the short call, not compounded.
    assert strat.generate_signal(_rich_iv_context(portfolio_delta=0.45)).direction == 1


def test_contract_count_scales_the_projected_exposure() -> None:
    caps = GreeksExposureCaps(max_abs_delta=0.5, max_abs_vega=1e6)
    strat = OptionsCarryStrategy(greeks_caps=caps)
    assert strat.generate_signal(_rich_iv_context(contracts=1.0)).direction == 1
    assert strat.generate_signal(_rich_iv_context(contracts=100.0)).direction == 0


def test_unmeasurable_contract_terms_fail_closed() -> None:
    """Caps configured but no terms to price against — suppress, do not assume safe."""
    strat = OptionsCarryStrategy(greeks_caps=GreeksExposureCaps(1e6, 1e6))
    blind = OptionsCarryContext(implied_vol_zscore=3.0, holding_direction=1)
    assert strat.generate_signal(blind).direction == 0


def test_gate_does_not_run_when_iv_is_not_rich() -> None:
    strat = OptionsCarryStrategy(greeks_caps=GreeksExposureCaps(1e6, 1e6))
    flat = _rich_iv_context(implied_vol_zscore=0.0)
    assert strat.generate_signal(flat).direction == 0
