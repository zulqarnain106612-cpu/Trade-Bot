"""Tests for src/tuning/live_overrides.py -- the seam that surfaces promoted
self-tuning values to the live regime/risk/features/model code paths."""

from __future__ import annotations

import pytest

from src.config import FeatureSettings, HMMSettings, RiskSettings, XGBoostSettings, get_settings
from src.tuning.live_overrides import (
    effective_feature_settings,
    effective_hmm_settings,
    effective_risk_settings,
    effective_xgboost_settings,
)
from src.tuning.registry import TunableParameter, parameter_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    parameter_registry._params.clear()
    yield
    parameter_registry._params.clear()


def _register(
    name: str, current: float, floor: float | None = None, ceiling: float | None = None
) -> None:
    parameter_registry.register(
        TunableParameter(
            name=name,
            description="test",
            floor=floor if floor is not None else current - abs(current) - 1,
            ceiling=ceiling if ceiling is not None else current + abs(current) + 1,
            current=current,
            eval_strategy="test",
        )
    )


class TestHMMOverlay:
    def test_no_registration_returns_base_unchanged(self) -> None:
        base = HMMSettings(entropy_threshold=0.5, entropy_scalar_floor=0.2)
        assert effective_hmm_settings(base) is base

    def test_default_base_reads_get_settings(self) -> None:
        assert effective_hmm_settings() == get_settings().hmm

    def test_entropy_threshold_overlaid(self) -> None:
        base = HMMSettings(entropy_threshold=0.5, entropy_scalar_floor=0.2)
        _register("hmm.entropy_threshold", 0.65)
        result = effective_hmm_settings(base)
        assert result.entropy_threshold == 0.65
        assert result.entropy_scalar_floor == 0.2  # untouched -- not registered

    def test_both_hmm_fields_overlaid(self) -> None:
        base = HMMSettings(entropy_threshold=0.5, entropy_scalar_floor=0.2)
        _register("hmm.entropy_threshold", 0.65)
        _register("hmm.entropy_scalar_floor", 0.3)
        result = effective_hmm_settings(base)
        assert result.entropy_threshold == 0.65
        assert result.entropy_scalar_floor == 0.3


class TestRiskOverlay:
    def test_no_registration_returns_base_unchanged(self) -> None:
        base = RiskSettings(slippage_impact_coeff_bps=10.0)
        assert effective_risk_settings(base) is base

    def test_slippage_coeff_overlaid(self) -> None:
        base = RiskSettings(slippage_impact_coeff_bps=10.0)
        _register("risk.slippage_impact_coeff_bps", 15.5)
        result = effective_risk_settings(base)
        assert result.slippage_impact_coeff_bps == 15.5

    def test_excluded_risk_fields_never_touched(self) -> None:
        """EXCLUDED_PARAMS (Kelly sizing, drawdown halts, ...) can never be
        registered at all (registry.py enforces this), so there is no code
        path here that could overlay them -- this just documents that
        effective_risk_settings() only ever names slippage_impact_coeff_bps."""
        base = RiskSettings(kelly_multiplier=0.5, slippage_impact_coeff_bps=10.0)
        result = effective_risk_settings(base)
        assert result.kelly_multiplier == 0.5


class TestFeatureOverlay:
    def test_no_registration_returns_base_unchanged(self) -> None:
        base = FeatureSettings(atr_window=14)
        assert effective_feature_settings(base) is base

    def test_window_field_overlaid_and_rounded(self) -> None:
        base = FeatureSettings(atr_window=14)
        _register("features.atr_window", 17.6)
        result = effective_feature_settings(base)
        assert result.atr_window == 18  # round(17.6)
        assert isinstance(result.atr_window, int)

    def test_window_field_clamped_to_minimum_two(self) -> None:
        base = FeatureSettings(atr_window=14)
        _register("features.atr_window", 1.2, floor=1.0, ceiling=20.0)
        result = effective_feature_settings(base)
        assert result.atr_window == 2  # max(2, round(1.2))

    def test_all_five_window_fields_overlaid(self) -> None:
        base = FeatureSettings()
        for field_name in (
            "vwap_window",
            "ofi_window",
            "atr_window",
            "sharpe_window",
            "volume_zscore_window",
        ):
            _register(f"features.{field_name}", 25.0)
        result = effective_feature_settings(base)
        assert result.vwap_window == 25
        assert result.ofi_window == 25
        assert result.atr_window == 25
        assert result.sharpe_window == 25
        assert result.volume_zscore_window == 25
        # Fields never registered for self-tuning stay untouched.
        assert result.realized_vol_window_short == base.realized_vol_window_short


class TestXGBoostOverlay:
    def test_no_registration_returns_base_unchanged(self) -> None:
        base = XGBoostSettings(max_depth=6)
        assert effective_xgboost_settings(base) is base

    def test_int_field_rounded(self) -> None:
        base = XGBoostSettings(max_depth=6)
        _register("xgboost.max_depth", 7.8, floor=1.0, ceiling=20.0)
        result = effective_xgboost_settings(base)
        assert result.max_depth == 8
        assert isinstance(result.max_depth, int)

    def test_float_field_not_rounded(self) -> None:
        base = XGBoostSettings(learning_rate=0.05)
        _register("xgboost.learning_rate", 0.0734, floor=1e-5, ceiling=1.0)
        result = effective_xgboost_settings(base)
        assert result.learning_rate == pytest.approx(0.0734)

    def test_all_int_fields_use_int_rounding(self) -> None:
        for field_name in ("n_estimators", "max_depth", "min_child_weight"):
            _register(f"xgboost.{field_name}", 12.4, floor=1.0, ceiling=2000.0)
        result = effective_xgboost_settings(XGBoostSettings())
        assert result.n_estimators == 12
        assert result.max_depth == 12
        assert result.min_child_weight == 12
