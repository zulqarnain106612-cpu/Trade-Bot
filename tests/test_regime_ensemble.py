"""Tests for the v4 regime ensemble combiner."""

from __future__ import annotations

import pytest

from src.regime.ensemble import RegimeEnsembleVote, combine_regime_votes


def test_passthrough_hmm_probabilities() -> None:
    vote = RegimeEnsembleVote(0.7, 0.2, 0.1, changepoint_probability=0.05)
    result = combine_regime_votes(vote)
    assert result.prob_trending == 0.7
    assert result.prob_ranging == 0.2
    assert result.prob_volatile == 0.1
    assert result.changepoint_probability == 0.05


def test_high_agreement_when_hmm_confident_and_changepoint_low() -> None:
    vote = RegimeEnsembleVote(0.9, 0.05, 0.05, changepoint_probability=0.05)
    result = combine_regime_votes(vote)
    assert result.agreement_score > 0.9


def test_low_agreement_when_hmm_confident_but_changepoint_high() -> None:
    vote = RegimeEnsembleVote(0.9, 0.05, 0.05, changepoint_probability=0.9)
    result = combine_regime_votes(vote)
    assert result.agreement_score < 0.3


def test_high_agreement_when_hmm_uncertain_and_changepoint_high() -> None:
    vote = RegimeEnsembleVote(0.34, 0.33, 0.33, changepoint_probability=0.7)
    result = combine_regime_votes(vote)
    assert result.agreement_score > 0.5


def test_rejects_out_of_range_probability() -> None:
    with pytest.raises(ValueError, match="hmm_prob_trending"):
        combine_regime_votes(RegimeEnsembleVote(1.5, 0.0, 0.0, 0.0))
    with pytest.raises(ValueError, match="changepoint_probability"):
        combine_regime_votes(RegimeEnsembleVote(0.5, 0.3, 0.2, -0.1))


def test_agreement_score_bounded() -> None:
    vote = RegimeEnsembleVote(1.0, 0.0, 0.0, changepoint_probability=1.0)
    result = combine_regime_votes(vote)
    assert 0.0 <= result.agreement_score <= 1.0
