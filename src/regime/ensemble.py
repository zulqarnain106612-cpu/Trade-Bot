"""
Regime ensemble — v4 Adaptive Regime & Model Layer.

Combines the existing HMM regime detector (src/regime/detector.py) with
the new Bayesian online changepoint detector (src/regime/changepoint.py)
into a single confidence-weighted signal, instead of trusting either model
alone. Per the Domain Prior (treat HMM transitions as probabilistic; avoid
hard-coded regime logic), this ensemble outputs continuous agreement/
disagreement scores, never a hard override of one model by another.

Authority:
  - Hamilton (1989) — HMM regime probabilities
  - Adams & MacKay (2007) — Bayesian online changepoint detection
  - Dietterich (2000) "Ensemble Methods in Machine Learning" — combining
    model votes to reduce variance vs. any single model
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegimeEnsembleVote:
    """One model's vote: regime confidence + a separate instability signal."""

    hmm_prob_trending: float
    hmm_prob_ranging: float
    hmm_prob_volatile: float
    changepoint_probability: float


@dataclass(frozen=True, slots=True)
class RegimeEnsembleResult:
    """
    Combined ensemble output.

    agreement_score in [0, 1]: how much the changepoint detector corroborates
    the HMM's current regime read. Low agreement (high changepoint prob while
    HMM reports low regime uncertainty) is itself a signal that the HMM may
    be lagging a real structural shift.
    """

    prob_trending: float
    prob_ranging: float
    prob_volatile: float
    changepoint_probability: float
    agreement_score: float


def combine_regime_votes(vote: RegimeEnsembleVote) -> RegimeEnsembleResult:
    """
    Pure combination function: HMM probabilities pass through unchanged
    (the ensemble never overrides HMM state), but agreement_score flags
    when the changepoint detector disagrees with HMM's implied stability.

    HMM "confidence" is proxied by max(prob_trending, prob_ranging,
    prob_volatile) — a peaked distribution means HMM is confident in one
    state; a flat distribution means HMM itself is uncertain.
    """
    for name, p in (
        ("hmm_prob_trending", vote.hmm_prob_trending),
        ("hmm_prob_ranging", vote.hmm_prob_ranging),
        ("hmm_prob_volatile", vote.hmm_prob_volatile),
        ("changepoint_probability", vote.changepoint_probability),
    ):
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"{name} must be in [0, 1], got {p}")

    hmm_confidence = max(vote.hmm_prob_trending, vote.hmm_prob_ranging, vote.hmm_prob_volatile)
    # Agreement is high when HMM is confident AND changepoint prob is low
    # (both models say "stable regime"), or when HMM is uncertain AND
    # changepoint prob is high (both models say "something is shifting").
    # Disagreement (low agreement_score) is the informative case: HMM
    # confident but changepoint detector screaming a shift just happened.
    agreement = 1.0 - abs(hmm_confidence - (1.0 - vote.changepoint_probability))

    return RegimeEnsembleResult(
        prob_trending=vote.hmm_prob_trending,
        prob_ranging=vote.hmm_prob_ranging,
        prob_volatile=vote.hmm_prob_volatile,
        changepoint_probability=vote.changepoint_probability,
        agreement_score=max(0.0, min(1.0, agreement)),
    )
