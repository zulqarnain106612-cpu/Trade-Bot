"""Tests for src/strategies/bootstrap.py — strategy registry population."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import StrategyPortfolioSettings
from src.strategies.bootstrap import (
    _SPECS,
    enabled_specs,
    register_default_strategies,
    total_enabled_fraction,
)
from src.strategies.registry import StrategyProtocol, StrategyRegistry


def _cfg(**overrides: object) -> StrategyPortfolioSettings:
    """Portfolio config with everything off unless explicitly enabled."""
    base: dict[str, object] = {spec.enabled_attr: False for spec in _SPECS}
    base.update(overrides)
    return StrategyPortfolioSettings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Spec table integrity
# ---------------------------------------------------------------------------


def test_every_spec_id_is_unique():
    ids = [spec.strategy_id for spec in _SPECS]
    assert len(ids) == len(set(ids))


def test_every_spec_maps_to_real_config_fields():
    cfg = StrategyPortfolioSettings()
    for spec in _SPECS:
        assert hasattr(cfg, spec.enabled_attr), spec.enabled_attr
        assert hasattr(cfg, spec.fraction_attr), spec.fraction_attr


def test_factory_produces_protocol_conformant_strategy_with_matching_id():
    for spec in _SPECS:
        strategy = spec.factory(0.1)
        assert isinstance(strategy, StrategyProtocol)
        assert strategy.strategy_id == spec.strategy_id
        assert strategy.required_capital_fraction() == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_enable_only_the_incumbent_signal_engine():
    # The v2/v3/v5 families have no out-of-sample validation yet; enabling
    # one by default would dilute capital away from the validated path.
    enabled = [spec.strategy_id for spec in enabled_specs(StrategyPortfolioSettings())]
    assert enabled == ["signal_engine_v1"]


def test_default_ceilings_allow_the_whole_book_to_be_deployed():
    # Ceilings are per-strategy caps, not a partition — the allocator caps
    # and renormalises. Summing below 1.0 would strand capital.
    assert total_enabled_fraction(StrategyPortfolioSettings()) >= 1.0


# ---------------------------------------------------------------------------
# enabled_specs / total_enabled_fraction
# ---------------------------------------------------------------------------


def test_enabled_specs_filters_disabled():
    cfg = _cfg(breakout_enabled=True, funding_carry_enabled=True)
    assert [s.strategy_id for s in enabled_specs(cfg)] == [
        "breakout_volume_v1",
        "funding_carry_v1",
    ]


def test_enabled_specs_empty_when_all_disabled():
    assert enabled_specs(_cfg()) == ()


def test_total_enabled_fraction_sums_only_enabled():
    cfg = _cfg(breakout_enabled=True, breakout_fraction=0.2, funding_carry_fraction=0.9)
    assert total_enabled_fraction(cfg) == pytest.approx(0.2)


def test_total_enabled_fraction_zero_when_all_disabled():
    assert total_enabled_fraction(_cfg()) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# register_default_strategies
# ---------------------------------------------------------------------------


def test_registers_enabled_strategies_into_registry():
    registry = StrategyRegistry()
    cfg = _cfg(signal_engine_enabled=True, breakout_enabled=True, breakout_fraction=0.15)
    registered = register_default_strategies(registry, cfg)
    assert registered == ("signal_engine_v1", "breakout_volume_v1")
    assert len(registry) == 2
    assert "breakout_volume_v1" in registry


def test_registered_strategy_carries_configured_capital_fraction():
    registry = StrategyRegistry()
    register_default_strategies(registry, _cfg(breakout_enabled=True, breakout_fraction=0.07))
    strategy = registry.get("breakout_volume_v1")
    assert strategy is not None
    assert strategy.required_capital_fraction() == pytest.approx(0.07)


def test_disabled_strategies_are_not_registered():
    registry = StrategyRegistry()
    register_default_strategies(registry, _cfg(breakout_enabled=True))
    assert "funding_carry_v1" not in registry
    assert "signal_engine_v1" not in registry


def test_registering_nothing_is_not_an_error():
    registry = StrategyRegistry()
    assert register_default_strategies(registry, _cfg()) == ()
    assert len(registry) == 0


def test_is_idempotent_across_repeat_calls():
    registry = StrategyRegistry()
    cfg = _cfg(signal_engine_enabled=True, breakout_enabled=True)
    first = register_default_strategies(registry, cfg)
    second = register_default_strategies(registry, cfg)
    assert first == ("signal_engine_v1", "breakout_volume_v1")
    assert second == ()  # already present — reported as newly-registered: none
    assert len(registry) == 2


def test_ceilings_may_exceed_one_because_they_are_caps_not_a_partition():
    # required_capital_fraction() is an upper bound per strategy; the
    # allocator caps each and renormalises to <= 1.0. Two strategies each
    # allowed up to 100% of the book is a legitimate config.
    registry = StrategyRegistry()
    cfg = _cfg(
        signal_engine_enabled=True,
        signal_engine_fraction=1.0,
        breakout_enabled=True,
        breakout_fraction=1.0,
    )
    assert len(register_default_strategies(registry, cfg)) == 2


def test_ceilings_below_full_book_still_register():
    registry = StrategyRegistry()
    cfg = _cfg(breakout_enabled=True, breakout_fraction=0.15)
    assert register_default_strategies(registry, cfg) == ("breakout_volume_v1",)
    assert len(registry) == 1


def test_defaults_to_process_registry_and_settings(monkeypatch):
    import src.strategies.bootstrap as bootstrap_mod
    from src.strategies.registry import get_default_registry

    # Substitute a throwaway registry so this test cannot leak strategies
    # into the process-wide one and make other tests order-dependent.
    registry_holder = StrategyRegistry()
    monkeypatch.setattr(bootstrap_mod, "get_default_registry", lambda: registry_holder)

    registered = register_default_strategies()
    assert registered == ("signal_engine_v1",)  # config defaults
    assert "signal_engine_v1" in registry_holder
    # The real process registry is untouched by this test.
    assert get_default_registry() is not registry_holder


def test_all_families_can_be_enabled_together_within_budget():
    # Every family on, fractions trimmed to fit — proves no spec in the
    # table is broken (bad factory, wrong id, non-conformant type).
    cfg = _cfg(
        **{spec.enabled_attr: True for spec in _SPECS},
        signal_engine_fraction=0.3,
        mean_reversion_fraction=0.1,
        breakout_fraction=0.1,
        funding_carry_fraction=0.1,
        xsec_momentum_fraction=0.1,
        basis_trade_fraction=0.1,
        cross_exchange_arb_fraction=0.1,
        options_carry_fraction=0.1,
    )
    registry = StrategyRegistry()
    registered = register_default_strategies(registry, cfg)
    assert len(registered) == len(_SPECS)
    assert len(registry) == len(_SPECS)


# ---------------------------------------------------------------------------
# Greeks-capped options-carry builder
# ---------------------------------------------------------------------------


def test_options_carry_built_without_caps_by_default() -> None:
    registry = StrategyRegistry()
    register_default_strategies(registry, _cfg(options_carry_enabled=True))
    strategy = registry.get("options_carry_v1")
    assert strategy._greeks_caps is None


def test_options_carry_receives_configured_greeks_caps() -> None:
    registry = StrategyRegistry()
    cfg = _cfg(
        options_carry_enabled=True,
        options_carry_max_abs_delta=2.5,
        options_carry_max_abs_vega=400.0,
    )
    register_default_strategies(registry, cfg)
    caps = registry.get("options_carry_v1")._greeks_caps
    assert caps is not None
    assert caps.max_abs_delta == 2.5
    assert caps.max_abs_vega == 400.0


def test_half_configured_greeks_caps_rejected_at_startup() -> None:
    with pytest.raises(ValidationError, match="must be set together"):
        _cfg(options_carry_enabled=True, options_carry_max_abs_delta=2.5)
    with pytest.raises(ValidationError, match="must be set together"):
        _cfg(options_carry_enabled=True, options_carry_max_abs_vega=400.0)


def test_specs_without_a_builder_still_use_the_plain_factory() -> None:
    registry = StrategyRegistry()
    register_default_strategies(registry, _cfg(breakout_enabled=True))
    assert registry.get("breakout_volume_v1").required_capital_fraction() > 0.0
