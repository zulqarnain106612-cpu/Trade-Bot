import pytest

from src.config import Settings, invalidate_settings_cache
from src.tuning.bootstrap import (
    FEATURE_WINDOW_FIELDS,
    XGBOOST_HYPERPARAM_FIELDS,
    register_feature_window_param,
    register_hmm_entropy_scalar_floor,
    register_hmm_entropy_threshold,
    register_slippage_impact_coeff,
    register_xgboost_hyperparam_param,
)
from src.tuning.registry import DuplicateParameterError, ParameterRegistry


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    invalidate_settings_cache()
    yield
    invalidate_settings_cache()


def test_register_hmm_entropy_threshold_uses_current_settings_as_champion() -> None:
    registry = ParameterRegistry()
    settings = Settings()
    param = register_hmm_entropy_threshold(registry, settings)
    assert param.current == settings.hmm.entropy_threshold
    assert param.floor < param.current < param.ceiling
    assert registry.is_registered("hmm.entropy_threshold")


def test_register_hmm_entropy_threshold_bounds_clamped_to_valid_range() -> None:
    registry = ParameterRegistry()
    settings = Settings(hmm={"entropy_threshold": 0.95})
    param = register_hmm_entropy_threshold(registry, settings)
    assert param.ceiling <= 1.0
    assert param.floor >= 0.0


def test_register_hmm_entropy_threshold_twice_raises() -> None:
    registry = ParameterRegistry()
    settings = Settings()
    register_hmm_entropy_threshold(registry, settings)
    with pytest.raises(DuplicateParameterError):
        register_hmm_entropy_threshold(registry, settings)


def test_register_hmm_entropy_scalar_floor() -> None:
    registry = ParameterRegistry()
    settings = Settings()
    param = register_hmm_entropy_scalar_floor(registry, settings)
    assert param.current == settings.hmm.entropy_scalar_floor
    assert registry.is_registered("hmm.entropy_scalar_floor")


def test_register_slippage_impact_coeff() -> None:
    registry = ParameterRegistry()
    settings = Settings()
    param = register_slippage_impact_coeff(registry, settings)
    assert param.current == settings.risk.slippage_impact_coeff_bps
    assert registry.is_registered("risk.slippage_impact_coeff_bps")
    assert param.ceiling <= 2000.0


def test_registering_all_three_does_not_collide() -> None:
    registry = ParameterRegistry()
    settings = Settings()
    register_hmm_entropy_threshold(registry, settings)
    register_hmm_entropy_scalar_floor(registry, settings)
    register_slippage_impact_coeff(registry, settings)
    assert len(registry.list_all()) == 3


@pytest.mark.parametrize("field_name", sorted(FEATURE_WINDOW_FIELDS))
def test_register_feature_window_param(field_name: str) -> None:
    registry = ParameterRegistry()
    settings = Settings()
    param = register_feature_window_param(registry, field_name, settings)
    assert param.current == getattr(settings.features, field_name)
    assert param.name == f"features.{field_name}"
    assert registry.is_registered(f"features.{field_name}")
    assert param.floor >= 2.0


def test_register_feature_window_param_unknown_field_raises() -> None:
    registry = ParameterRegistry()
    settings = Settings()
    with pytest.raises(ValueError, match="not a registered feature-window field"):
        register_feature_window_param(registry, "not_a_real_field", settings)


def test_register_all_feature_window_params_does_not_collide() -> None:
    registry = ParameterRegistry()
    settings = Settings()
    for field_name in FEATURE_WINDOW_FIELDS:
        register_feature_window_param(registry, field_name, settings)
    assert len(registry.list_all()) == len(FEATURE_WINDOW_FIELDS)


@pytest.mark.parametrize("field_name", sorted(XGBOOST_HYPERPARAM_FIELDS))
def test_register_xgboost_hyperparam_param(field_name: str) -> None:
    registry = ParameterRegistry()
    settings = Settings()
    param = register_xgboost_hyperparam_param(registry, field_name, settings)
    assert param.current == getattr(settings.xgboost, field_name)
    assert param.name == f"xgboost.{field_name}"
    assert registry.is_registered(f"xgboost.{field_name}")
    assert param.floor <= param.current <= param.ceiling


def test_register_xgboost_hyperparam_param_unknown_field_raises() -> None:
    registry = ParameterRegistry()
    settings = Settings()
    with pytest.raises(ValueError, match="not a registered XGBoost hyperparameter field"):
        register_xgboost_hyperparam_param(registry, "not_a_real_field", settings)


def test_register_all_xgboost_hyperparam_params_does_not_collide() -> None:
    registry = ParameterRegistry()
    settings = Settings()
    for field_name in XGBOOST_HYPERPARAM_FIELDS:
        register_xgboost_hyperparam_param(registry, field_name, settings)
    assert len(registry.list_all()) == len(XGBOOST_HYPERPARAM_FIELDS)


def test_register_xgboost_max_depth_bounds_clamped_to_valid_range() -> None:
    registry = ParameterRegistry()
    settings = Settings(xgboost={"max_depth": 19})
    param = register_xgboost_hyperparam_param(registry, "max_depth", settings)
    assert param.ceiling <= 20.0
