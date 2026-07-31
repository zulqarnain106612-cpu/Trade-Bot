"""Tests for src/strategies/regime_strategy_selector.py"""

from __future__ import annotations

import pytest

from src.strategies.regime_strategy_selector import (
    STRATEGY_BREAKOUT,
    STRATEGY_MEAN_REVERSION,
    STRATEGY_NEUTRAL,
    StrategySelection,
    select_strategy,
    select_strategy_from_prediction,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGIME_RANGING = 0
REGIME_TRENDING = 1
REGIME_VOLATILE = 2


def _sel(
    regime: int = REGIME_TRENDING,
    confidence: float = 0.80,
    entropy: float = 0.20,
    is_transition: bool = False,
    **kwargs,
) -> StrategySelection:
    return select_strategy(
        regime_state=regime,
        confidence=confidence,
        entropy=entropy,
        is_transition=is_transition,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Happy-path: correct strategy per regime
# ---------------------------------------------------------------------------


def test_ranging_regime_selects_mean_reversion():
    result = _sel(regime=REGIME_RANGING)
    assert result.strategy == STRATEGY_MEAN_REVERSION
    assert result.reject_reason == ""


def test_trending_regime_selects_breakout():
    result = _sel(regime=REGIME_TRENDING)
    assert result.strategy == STRATEGY_BREAKOUT
    assert result.reject_reason == ""


def test_volatile_regime_returns_neutral():
    result = _sel(regime=REGIME_VOLATILE)
    assert result.strategy == STRATEGY_NEUTRAL
    assert "volatile" in result.reject_reason


# ---------------------------------------------------------------------------
# Entropy gate
# ---------------------------------------------------------------------------


def test_high_entropy_returns_neutral():
    result = _sel(entropy=0.90, max_entropy=0.75)
    assert result.strategy == STRATEGY_NEUTRAL
    assert "entropy" in result.reject_reason


def test_entropy_at_threshold_passes():
    result = _sel(entropy=0.75, max_entropy=0.75, confidence=0.80)
    assert result.strategy != STRATEGY_NEUTRAL


def test_entropy_just_above_threshold_blocked():
    result = _sel(entropy=0.751, max_entropy=0.75)
    assert result.strategy == STRATEGY_NEUTRAL


# ---------------------------------------------------------------------------
# Confidence gate
# ---------------------------------------------------------------------------


def test_low_confidence_returns_neutral():
    result = _sel(confidence=0.40, min_confidence=0.55)
    assert result.strategy == STRATEGY_NEUTRAL
    assert "confidence" in result.reject_reason


def test_confidence_at_threshold_passes():
    result = _sel(confidence=0.55, min_confidence=0.55, entropy=0.20)
    assert result.strategy != STRATEGY_NEUTRAL


def test_confidence_just_below_threshold_blocked():
    result = _sel(confidence=0.549, min_confidence=0.55)
    assert result.strategy == STRATEGY_NEUTRAL


# ---------------------------------------------------------------------------
# Transition guard
# ---------------------------------------------------------------------------


def test_transition_guard_enabled_blocks_on_transition():
    result = _sel(is_transition=True, transition_guard=True)
    assert result.strategy == STRATEGY_NEUTRAL
    assert "transition" in result.reject_reason


def test_transition_guard_disabled_passes_through():
    result = _sel(is_transition=True, transition_guard=False)
    assert result.strategy != STRATEGY_NEUTRAL


def test_no_transition_with_guard_enabled_passes():
    result = _sel(is_transition=False, transition_guard=True)
    assert result.strategy != STRATEGY_NEUTRAL


# ---------------------------------------------------------------------------
# Unknown regime
# ---------------------------------------------------------------------------


def test_unknown_regime_returns_neutral():
    result = _sel(regime=99)
    assert result.strategy == STRATEGY_NEUTRAL
    assert "unknown_regime" in result.reject_reason


# ---------------------------------------------------------------------------
# StrategySelection fields
# ---------------------------------------------------------------------------


def test_selection_carries_regime_state():
    result = _sel(regime=REGIME_RANGING, confidence=0.80, entropy=0.20)
    assert result.regime_state == REGIME_RANGING


def test_selection_carries_confidence_and_entropy():
    result = _sel(confidence=0.75, entropy=0.30)
    assert result.confidence == pytest.approx(0.75)
    assert result.entropy == pytest.approx(0.30)


def test_selection_is_frozen():
    result = _sel()
    with pytest.raises((AttributeError, TypeError)):
        result.strategy = "other"  # type: ignore[misc]


def test_neutral_has_nonempty_reject_reason():
    result = _sel(regime=REGIME_VOLATILE)
    assert result.strategy == STRATEGY_NEUTRAL
    assert len(result.reject_reason) > 0


def test_non_neutral_has_empty_reject_reason():
    result = _sel(regime=REGIME_RANGING, confidence=0.90, entropy=0.10)
    assert result.strategy != STRATEGY_NEUTRAL
    assert result.reject_reason == ""


# ---------------------------------------------------------------------------
# select_strategy_from_prediction (duck-typed convenience)
# ---------------------------------------------------------------------------


class _FakePrediction:
    def __init__(self, state, dominant_prob, entropy, is_transition):
        self.state = state
        self.dominant_prob = dominant_prob
        self.entropy = entropy
        self.is_transition = is_transition


def test_from_prediction_ranging():
    pred = _FakePrediction(
        state=REGIME_RANGING, dominant_prob=0.80, entropy=0.15, is_transition=False
    )
    result = select_strategy_from_prediction(pred)
    assert result.strategy == STRATEGY_MEAN_REVERSION


def test_from_prediction_trending():
    pred = _FakePrediction(
        state=REGIME_TRENDING, dominant_prob=0.85, entropy=0.10, is_transition=False
    )
    result = select_strategy_from_prediction(pred)
    assert result.strategy == STRATEGY_BREAKOUT


def test_from_prediction_volatile():
    pred = _FakePrediction(
        state=REGIME_VOLATILE, dominant_prob=0.90, entropy=0.05, is_transition=False
    )
    result = select_strategy_from_prediction(pred)
    assert result.strategy == STRATEGY_NEUTRAL


def test_from_prediction_transition_blocked():
    pred = _FakePrediction(
        state=REGIME_TRENDING, dominant_prob=0.70, entropy=0.30, is_transition=True
    )
    result = select_strategy_from_prediction(pred, transition_guard=True)
    assert result.strategy == STRATEGY_NEUTRAL


def test_from_prediction_kwargs_forwarded():
    pred = _FakePrediction(
        state=REGIME_TRENDING, dominant_prob=0.40, entropy=0.20, is_transition=False
    )
    # min_confidence raised to 0.6 so 0.40 is blocked
    result = select_strategy_from_prediction(pred, min_confidence=0.60)
    assert result.strategy == STRATEGY_NEUTRAL


# ---------------------------------------------------------------------------
# Order of gates (entropy checked before confidence)
# ---------------------------------------------------------------------------


def test_entropy_gate_checked_before_confidence():
    # Both conditions fail; only entropy reason should appear (entropy checked first)
    result = _sel(entropy=0.95, max_entropy=0.75, confidence=0.30, min_confidence=0.55)
    assert "entropy" in result.reject_reason
    assert "confidence" not in result.reject_reason


def test_confidence_gate_checked_after_entropy():
    # Entropy passes, confidence fails
    result = _sel(entropy=0.20, max_entropy=0.75, confidence=0.30, min_confidence=0.55)
    assert "confidence" in result.reject_reason
