"""
Pure-logic tests for exchange intelligence provider modules.

Tests the _compute_stress_score staticmethod (identical formula across all
three providers) and the aggregator _is_neutral helper. No exchange
connectivity or async I/O required.
"""

from __future__ import annotations

from src.intelligence.providers.aggregator import _is_neutral
from src.intelligence.providers.binance_provider import BinanceIntelligenceProvider
from src.intelligence.providers.bybit_provider import BybitIntelligenceProvider
from src.intelligence.providers.okx_provider import OKXIntelligenceProvider


# ---------------------------------------------------------------------------
# _compute_stress_score — shared across all three providers
# ---------------------------------------------------------------------------


class TestComputeStressScore:
    """
    Covers BybitIntelligenceProvider, BinanceIntelligenceProvider, and
    OKXIntelligenceProvider._compute_stress_score (identical formula).
    """

    def test_zero_inputs_returns_zero_bybit(self) -> None:
        assert BybitIntelligenceProvider._compute_stress_score(0.0, 0.0, 0.0) == 0.0

    def test_zero_inputs_returns_zero_binance(self) -> None:
        assert BinanceIntelligenceProvider._compute_stress_score(0.0, 0.0, 0.0) == 0.0

    def test_zero_inputs_returns_zero_okx(self) -> None:
        assert OKXIntelligenceProvider._compute_stress_score(0.0, 0.0, 0.0) == 0.0

    def test_output_clamped_to_unit_interval(self) -> None:
        # Extreme inputs — all components saturate to 1.0
        score = BybitIntelligenceProvider._compute_stress_score(1000.0, 100.0, -100.0)
        assert score == 1.0

    def test_output_never_negative(self) -> None:
        # Positive OI change → oi_stress=0, no negative contribution
        score = BybitIntelligenceProvider._compute_stress_score(-5.0, -0.5, 10.0)
        assert score >= 0.0

    def test_basis_stress_only(self) -> None:
        # 100 bps basis → basis_stress=1.0 → score = _W_BASIS = 0.35
        score = BybitIntelligenceProvider._compute_stress_score(100.0, 0.0, 0.0)
        assert abs(score - 0.35) < 1e-4

    def test_funding_stress_only(self) -> None:
        # 3.0 z-score → funding_stress=1.0 → score = _W_FR_Z = 0.40
        score = BybitIntelligenceProvider._compute_stress_score(0.0, 3.0, 0.0)
        assert abs(score - 0.40) < 1e-4

    def test_oi_stress_only_positive_oi_change_no_stress(self) -> None:
        # Positive OI change → oi_stress = 0 (only negative OI change is stressful)
        score = BybitIntelligenceProvider._compute_stress_score(0.0, 0.0, 10.0)
        assert score == 0.0

    def test_oi_stress_only_negative_oi_change(self) -> None:
        # -5% OI change → oi_stress=1.0 → score = _W_OI = 0.25
        score = BybitIntelligenceProvider._compute_stress_score(0.0, 0.0, -5.0)
        assert abs(score - 0.25) < 1e-4

    def test_negative_basis_same_as_positive(self) -> None:
        # abs() applied to basis_bps
        pos = BybitIntelligenceProvider._compute_stress_score(50.0, 0.0, 0.0)
        neg = BybitIntelligenceProvider._compute_stress_score(-50.0, 0.0, 0.0)
        assert pos == neg

    def test_negative_funding_zscore_same_as_positive(self) -> None:
        # abs() applied to funding_zscore
        pos = BybitIntelligenceProvider._compute_stress_score(0.0, 2.0, 0.0)
        neg = BybitIntelligenceProvider._compute_stress_score(0.0, -2.0, 0.0)
        assert pos == neg

    def test_all_three_providers_identical_formula(self) -> None:
        basis_bps, funding_z, oi_pct = 45.0, 1.5, -2.0
        bybit = BybitIntelligenceProvider._compute_stress_score(basis_bps, funding_z, oi_pct)
        binance = BinanceIntelligenceProvider._compute_stress_score(basis_bps, funding_z, oi_pct)
        okx = OKXIntelligenceProvider._compute_stress_score(basis_bps, funding_z, oi_pct)
        assert bybit == binance == okx

    def test_basis_stress_capped_at_1(self) -> None:
        # basis_bps=200 → abs/100=2.0, capped to 1.0
        score = BybitIntelligenceProvider._compute_stress_score(200.0, 0.0, 0.0)
        assert abs(score - 0.35) < 1e-4

    def test_funding_stress_capped_at_1(self) -> None:
        # z-score=10 → 10/3 > 1, capped to 1.0
        score = BybitIntelligenceProvider._compute_stress_score(0.0, 10.0, 0.0)
        assert abs(score - 0.40) < 1e-4

    def test_oi_stress_capped_at_1(self) -> None:
        # oi_change_pct=-50 → 50/5=10, capped to 1.0
        score = BybitIntelligenceProvider._compute_stress_score(0.0, 0.0, -50.0)
        assert abs(score - 0.25) < 1e-4

    def test_result_rounded_to_4_decimals(self) -> None:
        score = BybitIntelligenceProvider._compute_stress_score(33.0, 1.1, -1.5)
        assert score == round(score, 4)


# ---------------------------------------------------------------------------
# aggregator._is_neutral
# ---------------------------------------------------------------------------


class TestIsNeutral:
    def test_neutral_funding_rate_is_neutral(self) -> None:
        # funding_rate neutral = 0.0
        assert _is_neutral("funding_rate", 0.0) is True

    def test_non_neutral_funding_rate(self) -> None:
        assert _is_neutral("funding_rate", 0.001) is False

    def test_nan_is_not_neutral(self) -> None:
        # NaN is treated as missing, not neutral
        assert _is_neutral("funding_rate", float("nan")) is False

    def test_unknown_field_defaults_to_zero_neutral(self) -> None:
        # Unknown field: neutral defaults to 0.0
        assert _is_neutral("unknown_field", 0.0) is True
        assert _is_neutral("unknown_field", 1.0) is False

    def test_very_small_deviation_is_neutral(self) -> None:
        # Within 1e-9 epsilon → neutral
        assert _is_neutral("funding_rate", 1e-10) is True

    def test_just_outside_epsilon_is_not_neutral(self) -> None:
        assert _is_neutral("funding_rate", 1e-8) is False
