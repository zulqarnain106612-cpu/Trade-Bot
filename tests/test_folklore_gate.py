"""
Tests for :mod:`src.mathcore.folklore`.

The exit gate has two clauses. A strategy importing a Fibonacci retracement
feature must fail with a message naming the registry entry and the evidence
required; and ``ValidationEvidence`` must be impossible to satisfy without an
out-of-sample window, a hypothesis count, and a multiple-testing correction,
with each incomplete case asserted to refuse. Both are covered here.
"""

from __future__ import annotations

import pytest

from src.mathcore.folklore import (
    FolkloreError,
    ValidationEvidence,
    assert_feature_not_folklore,
    assert_not_folklore,
    detect_folklore,
)
from src.mathcore.folklore.gate import RegistryError
from src.mathcore.registry import load_registry

ALL_FOLKLORE_IDS = [e.id for e in load_registry().by_verdict("folklore")]


def _sufficient() -> ValidationEvidence:
    return ValidationEvidence(
        out_of_sample_days=500,
        hypotheses_tried=4,
        correction="bonferroni",
        raw_p_value=0.0005,
    )


# ---- the gate on registry ids ----------------------------------------------


@pytest.mark.parametrize("entry_id", ALL_FOLKLORE_IDS)
def test_every_folklore_entry_is_blocked_without_evidence(entry_id: str) -> None:
    with pytest.raises(FolkloreError):
        assert_not_folklore(entry_id)


def test_the_block_message_names_the_entry_and_the_required_evidence() -> None:
    with pytest.raises(FolkloreError) as excinfo:
        assert_not_folklore("fibonacci-retracement")
    message = str(excinfo.value)
    assert "fibonacci-retracement" in message
    assert "out-of-sample" in message
    assert "multiple-testing correction" in message


def test_a_load_bearing_entry_passes_the_gate() -> None:
    """The gate guards only the folklore verdict; real math is not blocked."""
    assert_not_folklore("finite-fields")
    assert_not_folklore("elliptic-curves")


def test_an_unknown_feature_id_raises_registry_error_not_folklore() -> None:
    """A typo must not read as 'validated'; it is a lookup error."""
    with pytest.raises(RegistryError):
        assert_not_folklore("fibbonaci-retracementt")


# ---- evidence lets a folklore feature through, if it is real ---------------


def test_sufficient_evidence_opens_the_gate() -> None:
    assert_not_folklore("fibonacci-retracement", _sufficient())


def test_insufficient_evidence_is_still_blocked_and_says_why() -> None:
    """0.04 from 50 tries: Bonferroni-corrected to ~1.0, still noise."""
    weak = ValidationEvidence(
        out_of_sample_days=500,
        hypotheses_tried=50,
        correction="bonferroni",
        raw_p_value=0.04,
    )
    assert not weak.is_sufficient
    with pytest.raises(FolkloreError, match="insufficient"):
        assert_not_folklore("fibonacci-retracement", weak)


# ---- ValidationEvidence cannot be built incomplete -------------------------


def test_evidence_requires_an_out_of_sample_window() -> None:
    with pytest.raises(ValueError, match="out_of_sample_days"):
        ValidationEvidence(
            out_of_sample_days=0,
            hypotheses_tried=3,
            correction="bonferroni",
            raw_p_value=0.001,
        )


def test_evidence_requires_a_hypothesis_count() -> None:
    with pytest.raises(ValueError, match="hypotheses_tried"):
        ValidationEvidence(
            out_of_sample_days=500,
            hypotheses_tried=0,
            correction="bonferroni",
            raw_p_value=0.001,
        )


def test_evidence_requires_a_known_multiple_testing_correction() -> None:
    with pytest.raises(ValueError, match="correction"):
        ValidationEvidence(
            out_of_sample_days=500,
            hypotheses_tried=3,
            correction="none",
            raw_p_value=0.001,
        )


def test_evidence_requires_a_valid_p_value() -> None:
    with pytest.raises(ValueError, match="raw_p_value"):
        ValidationEvidence(
            out_of_sample_days=500,
            hypotheses_tried=3,
            correction="bonferroni",
            raw_p_value=1.5,
        )


# ---- the corrections ------------------------------------------------------


def test_bonferroni_multiplies_by_the_hypothesis_count() -> None:
    ev = ValidationEvidence(
        out_of_sample_days=500, hypotheses_tried=10, correction="bonferroni", raw_p_value=0.001
    )
    assert ev.corrected_p_value == pytest.approx(0.01)


def test_sidak_is_close_to_bonferroni_for_small_p() -> None:
    bonf = ValidationEvidence(
        out_of_sample_days=500, hypotheses_tried=5, correction="bonferroni", raw_p_value=0.001
    )
    sidak = ValidationEvidence(
        out_of_sample_days=500, hypotheses_tried=5, correction="sidak", raw_p_value=0.001
    )
    assert sidak.corrected_p_value == pytest.approx(bonf.corrected_p_value, rel=0.01)
    assert sidak.corrected_p_value <= bonf.corrected_p_value


def test_correction_is_capped_at_one() -> None:
    ev = ValidationEvidence(
        out_of_sample_days=500, hypotheses_tried=1000, correction="bonferroni", raw_p_value=0.5
    )
    assert ev.corrected_p_value == 1.0


def test_more_hypotheses_never_lowers_the_corrected_p_value() -> None:
    values = [
        ValidationEvidence(
            out_of_sample_days=1, hypotheses_tried=m, correction="bonferroni", raw_p_value=0.001
        ).corrected_p_value
        for m in (1, 2, 5, 10, 100)
    ]
    assert all(a <= b for a, b in zip(values, values[1:], strict=False))


# ---- detection: the Fibonacci-import exit-gate clause ----------------------


def test_a_strategy_importing_a_fibonacci_feature_is_refused() -> None:
    """
    The literal exit-gate scenario: a feature named for Fibonacci retracement,
    registered by name, fails with the registry entry named in the message.
    """
    with pytest.raises(FolkloreError, match="fibonacci-retracement"):
        assert_feature_not_folklore("fib_618_retracement_signal")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("fib_618_retracement", "fibonacci-retracement"),
        ("golden_ratio_zone", "fibonacci-retracement"),
        ("elliott_wave_count", "elliott-wave"),
        ("gann_angle_45", "gann-methods"),
        ("s2f_ratio", "stock-to-flow"),
        ("stock_to_flow_deflection", "stock-to-flow"),
        ("rainbow_chart_band", "rainbow-and-power-law-charts"),
        ("lunar_phase_bias", "astro-lunar-trading"),
        ("meme_supply_magic", "meme-tokenomics-numbers"),
    ],
)
def test_folklore_aliases_map_to_their_registry_entry(name: str, expected: str) -> None:
    assert detect_folklore(name) == expected


@pytest.mark.parametrize(
    "name",
    ["rsi_14", "ema_200", "macd_signal", "atr_stop", "vwap", "ma_420", "fibre_optic_latency"],
)
def test_ordinary_features_are_not_flagged(name: str) -> None:
    assert detect_folklore(name) is None
    assert_feature_not_folklore(name)  # must not raise


def test_a_detected_folklore_feature_passes_with_sufficient_evidence() -> None:
    assert_feature_not_folklore("fib_retracement", _sufficient())


def test_detection_is_case_insensitive() -> None:
    assert detect_folklore("ELLIOTT_WAVE") == "elliott-wave"
    assert detect_folklore("Gann_Square_Of_Nine") == "gann-methods"
