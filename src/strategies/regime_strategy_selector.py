"""
Regime-conditional strategy selector.

Decides which trading strategy is appropriate for the current market regime,
following the practitioner principle that strategies should be matched to
regime conditions rather than applied blindly across all market states.

Regime -> strategy mapping (primary authority):
  RANGING  (0) -> mean_reversion  : Bollinger/OU signals have positive EV
                                      when price oscillates around a stable mean.
  TRENDING (1) -> breakout        : Donchian/ATR breakouts capture sustained
                                      directional moves (Chan 2013 Ch.2).
  VOLATILE (2) -> none            : No new positions during vol spikes; both
                                      mean-reversion and breakout models lose
                                      edge during regime breaks (Schwager 1984).

The selector also gates on regime confidence (entropy / dominant_prob).  A
low-confidence regime call (high entropy) returns NEUTRAL so the engine does
not commit to a strategy on an uncertain regime.

Authority:
  Chan (2013) Algorithmic Trading Ch.1-2 — regime-conditional strategy selection.
  Carver (2019) Systematic Trading Ch.7 — strategy diversification across regimes.
  Schwager (1984) Market Wizards — do not trade during regime uncertainty.
  Lopez de Prado (2018) AFML Ch.17 — regime-aware position sizing and strategy
    activation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Strategy identifiers returned by the selector
STRATEGY_MEAN_REVERSION: Final[str] = "mean_reversion"
STRATEGY_BREAKOUT: Final[str] = "breakout"
STRATEGY_FUNDING_CARRY: Final[str] = "funding_carry"
STRATEGY_NEUTRAL: Final[str] = "neutral"

# Default thresholds
_DEFAULT_MIN_CONFIDENCE: Final[float] = 0.55
_DEFAULT_MAX_ENTROPY: Final[float] = 0.75
_DEFAULT_TRANSITION_GUARD: Final[bool] = True


@dataclass(frozen=True)
class StrategySelection:
    """
    Result of a regime-conditional strategy selection.

    strategy       : one of the STRATEGY_* constants.
    regime_state   : raw regime integer (0=ranging, 1=trending, 2=volatile).
    confidence     : dominant regime probability in [0, 1].
    entropy        : normalised Shannon entropy in [0, 1] (0 = certain, 1 = uniform).
    is_transition  : True when BOCD/ensemble signals an active regime transition.
    reject_reason  : non-empty when strategy == STRATEGY_NEUTRAL; explains why.
    """

    strategy: str
    regime_state: int
    confidence: float
    entropy: float
    is_transition: bool
    reject_reason: str


def select_strategy(
    regime_state: int,
    confidence: float,
    entropy: float,
    is_transition: bool = False,
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
    max_entropy: float = _DEFAULT_MAX_ENTROPY,
    transition_guard: bool = _DEFAULT_TRANSITION_GUARD,
    allow_funding_carry: bool = True,
) -> StrategySelection:
    """
    Select the appropriate trading strategy for the current regime.

    Parameters
    ----------
    regime_state    : 0 (ranging), 1 (trending), 2 (volatile).
    confidence      : dominant regime probability in [0, 1].
                      Use EnsembleRegimePrediction.dominant_prob.
    entropy         : normalised regime entropy in [0, 1].
                      Use EnsembleRegimePrediction.entropy.
    is_transition   : True when ensemble detector signals a regime change.
                      Use EnsembleRegimePrediction.is_transition.
    min_confidence  : below this threshold, return NEUTRAL (uncertain regime).
    max_entropy     : above this threshold, return NEUTRAL (high uncertainty).
    transition_guard: when True, return NEUTRAL during detected transitions.
    allow_funding_carry: when True, funding_carry is available as an overlay
                      in trending or ranging (not volatile) regimes.

    Returns
    -------
    StrategySelection — frozen dataclass with the chosen strategy and diagnostics.
    """
    from src.config import REGIME_RANGING, REGIME_TRENDING, REGIME_VOLATILE

    # Gate 1: entropy / confidence checks
    if entropy > max_entropy:
        reason = f"high_entropy:{entropy:.3f}>{max_entropy}"
        _log_neutral(regime_state, confidence, entropy, reason)
        return StrategySelection(
            strategy=STRATEGY_NEUTRAL,
            regime_state=regime_state,
            confidence=confidence,
            entropy=entropy,
            is_transition=is_transition,
            reject_reason=reason,
        )

    if confidence < min_confidence:
        reason = f"low_confidence:{confidence:.3f}<{min_confidence}"
        _log_neutral(regime_state, confidence, entropy, reason)
        return StrategySelection(
            strategy=STRATEGY_NEUTRAL,
            regime_state=regime_state,
            confidence=confidence,
            entropy=entropy,
            is_transition=is_transition,
            reject_reason=reason,
        )

    # Gate 2: transition guard
    if transition_guard and is_transition:
        reason = "regime_transition_detected"
        _log_neutral(regime_state, confidence, entropy, reason)
        return StrategySelection(
            strategy=STRATEGY_NEUTRAL,
            regime_state=regime_state,
            confidence=confidence,
            entropy=entropy,
            is_transition=is_transition,
            reject_reason=reason,
        )

    # Gate 3: volatile regime — hard block
    if regime_state == REGIME_VOLATILE:
        reason = "volatile_regime"
        _log_neutral(regime_state, confidence, entropy, reason)
        return StrategySelection(
            strategy=STRATEGY_NEUTRAL,
            regime_state=regime_state,
            confidence=confidence,
            entropy=entropy,
            is_transition=is_transition,
            reject_reason=reason,
        )

    # Select strategy based on regime
    if regime_state == REGIME_RANGING:
        strategy = STRATEGY_MEAN_REVERSION
    elif regime_state == REGIME_TRENDING:
        strategy = STRATEGY_BREAKOUT
    else:
        # Unknown regime — conservative neutral
        reason = f"unknown_regime:{regime_state}"
        _log_neutral(regime_state, confidence, entropy, reason)
        return StrategySelection(
            strategy=STRATEGY_NEUTRAL,
            regime_state=regime_state,
            confidence=confidence,
            entropy=entropy,
            is_transition=is_transition,
            reject_reason=reason,
        )

    log.debug(
        "regime_strategy_selector.selected",
        strategy=strategy,
        regime_state=regime_state,
        confidence=round(confidence, 3),
        entropy=round(entropy, 3),
    )
    return StrategySelection(
        strategy=strategy,
        regime_state=regime_state,
        confidence=confidence,
        entropy=entropy,
        is_transition=is_transition,
        reject_reason="",
    )


def select_strategy_from_config(
    regime_state: int,
    confidence: float,
    entropy: float,
    is_transition: bool = False,
) -> StrategySelection:
    """
    Config-aware wrapper: reads thresholds from settings.strategy.*.

    Convenience alternative to select_strategy() for production use where
    the caller does not want to manage threshold values explicitly.
    Reads rs_min_confidence, rs_max_entropy, rs_transition_guard from
    src.config.get_settings().strategy.
    """
    from src.config import get_settings

    cfg = get_settings().strategy
    return select_strategy(
        regime_state=regime_state,
        confidence=confidence,
        entropy=entropy,
        is_transition=is_transition,
        min_confidence=cfg.rs_min_confidence,
        max_entropy=cfg.rs_max_entropy,
        transition_guard=cfg.rs_transition_guard,
    )


def select_strategy_from_prediction(prediction: object, **kwargs: object) -> StrategySelection:
    """
    Convenience wrapper accepting an EnsembleRegimePrediction directly.

    Reads .state, .dominant_prob, .entropy, .is_transition from the
    prediction object so the caller does not need to unpack it.

    Parameters
    ----------
    prediction : EnsembleRegimePrediction (duck-typed — any object with
                  .state, .dominant_prob, .entropy, .is_transition).
    **kwargs   : forwarded to select_strategy() (e.g. min_confidence,
                  allow_funding_carry).
    """
    return select_strategy(
        regime_state=prediction.state,  # type: ignore[union-attr]
        confidence=prediction.dominant_prob,  # type: ignore[union-attr]
        entropy=prediction.entropy,  # type: ignore[union-attr]
        is_transition=prediction.is_transition,  # type: ignore[union-attr]
        **kwargs,
    )


def _log_neutral(
    regime_state: int,
    confidence: float,
    entropy: float,
    reason: str,
) -> None:
    log.info(
        "regime_strategy_selector.neutral",
        regime_state=regime_state,
        confidence=round(confidence, 3),
        entropy=round(entropy, 3),
        reason=reason,
    )
