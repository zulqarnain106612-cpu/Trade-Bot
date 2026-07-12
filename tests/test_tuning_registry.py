import pytest

from src.tuning.registry import (
    DuplicateParameterError,
    ExcludedParameterError,
    InvalidBoundsError,
    ParameterRegistry,
    TunableParameter,
    UnknownParameterError,
)


def make_param(**overrides: object) -> TunableParameter:
    defaults: dict[str, object] = {
        "name": "hmm.entropy_threshold",
        "description": "Regime posterior entropy gate",
        "floor": 0.3,
        "ceiling": 0.7,
        "current": 0.5,
        "eval_strategy": "cpcv_oos_sharpe",
    }
    defaults.update(overrides)
    return TunableParameter(**defaults)  # type: ignore[arg-type]


def test_valid_parameter_constructs() -> None:
    p = make_param()
    assert p.name == "hmm.entropy_threshold"
    assert p.in_bounds(0.5)
    assert not p.in_bounds(0.9)


@pytest.mark.parametrize(
    "excluded_name",
    [
        "risk.kelly_multiplier",
        "risk.kelly_ceiling",
        "risk.daily_drawdown_halt_pct",
        "risk.consecutive_loss_halt",
        "risk.max_position_size_pct",
        "risk.notional_limit_usd",
        "trading_mode",
        "execution_mode",
        "binance.api_key",
    ],
)
def test_excluded_parameters_cannot_be_constructed(excluded_name: str) -> None:
    with pytest.raises(ExcludedParameterError):
        make_param(name=excluded_name)


def test_floor_greater_than_ceiling_rejected() -> None:
    with pytest.raises(InvalidBoundsError):
        make_param(floor=0.8, ceiling=0.2)


def test_current_outside_bounds_rejected() -> None:
    with pytest.raises(InvalidBoundsError):
        make_param(floor=0.3, ceiling=0.7, current=0.9)


def test_registry_register_and_get() -> None:
    registry = ParameterRegistry()
    param = make_param()
    registry.register(param)
    assert registry.get("hmm.entropy_threshold") is param
    assert registry.is_registered("hmm.entropy_threshold")
    assert registry.list_all() == [param]


def test_registry_duplicate_registration_rejected() -> None:
    registry = ParameterRegistry()
    registry.register(make_param())
    with pytest.raises(DuplicateParameterError):
        registry.register(make_param())


def test_registry_unknown_parameter_raises() -> None:
    registry = ParameterRegistry()
    with pytest.raises(UnknownParameterError):
        registry.get("does.not.exist")


def test_registry_unregister() -> None:
    registry = ParameterRegistry()
    registry.register(make_param())
    registry.unregister("hmm.entropy_threshold")
    assert not registry.is_registered("hmm.entropy_threshold")
    # idempotent
    registry.unregister("hmm.entropy_threshold")


def test_registry_starts_empty_singleton() -> None:
    from src.tuning.registry import parameter_registry

    # Phase 1 ships with zero live parameters registered against the singleton.
    assert parameter_registry.list_all() == []


def test_update_current_advances_champion_value() -> None:
    registry = ParameterRegistry()
    registry.register(make_param())
    updated = registry.update_current("hmm.entropy_threshold", 0.6)
    assert updated.current == 0.6
    # get() must return the SAME advanced value, not the stale original --
    # this is the seam TuningRunner.attempt() and src/tuning/live_overrides.py
    # depend on to see a promotion at all.
    assert registry.get("hmm.entropy_threshold").current == 0.6


def test_update_current_preserves_other_fields() -> None:
    registry = ParameterRegistry()
    registry.register(make_param())
    updated = registry.update_current("hmm.entropy_threshold", 0.6)
    assert updated.floor == 0.3
    assert updated.ceiling == 0.7
    assert updated.eval_strategy == "cpcv_oos_sharpe"


def test_update_current_unknown_parameter_raises() -> None:
    registry = ParameterRegistry()
    with pytest.raises(UnknownParameterError):
        registry.update_current("does.not.exist", 0.5)


def test_update_current_out_of_bounds_raises_and_leaves_champion_unchanged() -> None:
    registry = ParameterRegistry()
    registry.register(make_param(floor=0.3, ceiling=0.7, current=0.5))
    with pytest.raises(InvalidBoundsError):
        registry.update_current("hmm.entropy_threshold", 0.9)
    # A rejected update must not corrupt the previously valid champion.
    assert registry.get("hmm.entropy_threshold").current == 0.5
