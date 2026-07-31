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

    def test_ensemble_blend_weight_overlaid(self) -> None:
        base = RiskSettings(ensemble_blend_weight=0.15)
        _register("risk.ensemble_blend_weight", 0.25)
        result = effective_risk_settings(base)
        assert result.ensemble_blend_weight == 0.25

    def test_both_risk_fields_overlaid_independently(self) -> None:
        base = RiskSettings(slippage_impact_coeff_bps=10.0, ensemble_blend_weight=0.15)
        _register("risk.slippage_impact_coeff_bps", 15.5)
        _register("risk.ensemble_blend_weight", 0.3)
        result = effective_risk_settings(base)
        assert result.slippage_impact_coeff_bps == 15.5
        assert result.ensemble_blend_weight == 0.3

    def test_garch_vol_threshold_overlaid(self) -> None:
        base = RiskSettings(garch_vol_threshold=0.02)
        _register("risk.garch_vol_threshold", 0.035, floor=0.001, ceiling=0.50)
        result = effective_risk_settings(base)
        assert result.garch_vol_threshold == pytest.approx(0.035)

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

    def test_garch_window_overlaid_and_rounded(self) -> None:
        base = FeatureSettings(garch_window=60)
        _register("features.garch_window", 80.6, floor=50.0, ceiling=200.0)
        result = effective_feature_settings(base)
        assert result.garch_window == 81  # round(80.6)

    def test_garch_window_clamped_to_50_floor(self) -> None:
        """Promoted value below 50 must be clamped to 50 (garch_window ge=50).

        The registry floor is deliberately looser (2.0) than the overlay's
        garch-specific floor: TunableParameter now rejects a `current`
        outside [floor, ceiling], so registering current=30 against
        floor=50 is no longer constructible. The overlay clamp is the last
        line of defence when a parameter is registered with a permissive
        floor, which is exactly what this asserts.
        """
        base = FeatureSettings(garch_window=60)
        _register("features.garch_window", 30.0, floor=2.0, ceiling=200.0)
        result = effective_feature_settings(base)
        assert result.garch_window == 50  # max(50, round(30))

    def test_other_windows_still_use_floor_two(self) -> None:
        """Non-garch windows must not inherit garch's 50 floor."""
        base = FeatureSettings(atr_window=14)
        _register("features.atr_window", 1.4, floor=1.0, ceiling=200.0)
        result = effective_feature_settings(base)
        assert result.atr_window == 2  # max(2, round(1.4))


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
