"""
The HMM restart budget must survive a failing restart, and prefer converged fits.

Two problems in the multi-init loop:

1. `candidate.fit(...)` sat outside the try that guards `candidate.score(...)`.
   A degenerate covariance raises during EM at least as often as it does at
   the score call, and out there it aborted the entire loop -- one bad seed
   destroyed all n restarts, which is the exact failure the restart budget
   exists to absorb.

2. Selection was by log-likelihood alone. EM log-likelihood rises
   monotonically, so a restart that exhausted n_iter without converging can
   out-score a converged one while sitting on parameters the optimiser was
   still moving. Picking it also silently flipped the detector into the
   "not converged -> force VOLATILE" branch, so which regime the whole
   system saw came down to an accident of scoring.

These tests exercise the selection rule directly rather than driving
hmmlearn into a genuine degeneracy, which is not reliably reproducible.
"""

from __future__ import annotations

import pytest


class _Monitor:
    def __init__(self, converged: bool) -> None:
        self.converged = converged
        self.iter = 10


class _Candidate:
    """Stands in for a GaussianHMM restart."""

    def __init__(self, score: float, converged: bool, fit_raises: bool = False) -> None:
        self._score = score
        self.monitor_ = _Monitor(converged)
        self._fit_raises = fit_raises
        self.fitted = False

    def fit(self, X, lengths=None):
        if self._fit_raises:
            raise ValueError("degenerate covariance")
        self.fitted = True

    def score(self, X, lengths=None) -> float:
        if not self.fitted:
            raise RuntimeError("scored before fit")
        return self._score


def _select(candidates: list[_Candidate]):
    """Mirror RegimeDetector.fit()'s multi-init selection."""
    best_model = None
    best_score = float("-inf")
    failed: list[int] = []

    for seed, candidate in enumerate(candidates):
        try:
            candidate.fit(None)
            score = candidate.score(None)
        except Exception:
            failed.append(seed)
            continue

        candidate_converged = bool(candidate.monitor_.converged)
        best_converged = best_model is not None and bool(best_model.monitor_.converged)
        if best_model is None:
            better = True
        elif candidate_converged != best_converged:
            better = candidate_converged
        else:
            better = score > best_score
        if better:
            best_score = score
            best_model = candidate

    return best_model, failed


def test_a_restart_that_fails_to_fit_does_not_kill_the_others() -> None:
    good = _Candidate(score=-100.0, converged=True)
    best, failed = _select([_Candidate(0.0, True, fit_raises=True), good])

    assert best is good
    assert failed == [0]


def test_every_restart_failing_leaves_no_model() -> None:
    best, failed = _select([_Candidate(0.0, True, fit_raises=True) for _ in range(3)])

    assert best is None
    assert failed == [0, 1, 2]


def test_a_converged_fit_beats_a_higher_scoring_unconverged_one() -> None:
    converged = _Candidate(score=-500.0, converged=True)
    best, _ = _select([_Candidate(-10.0, converged=False), converged])

    assert best is converged


def test_order_does_not_change_that_preference() -> None:
    converged = _Candidate(score=-500.0, converged=True)
    best, _ = _select([converged, _Candidate(-10.0, converged=False)])

    assert best is converged


def test_among_converged_fits_the_best_likelihood_still_wins() -> None:
    top = _Candidate(score=-50.0, converged=True)
    best, _ = _select([_Candidate(-90.0, True), top, _Candidate(-70.0, True)])

    assert best is top


def test_among_unconverged_fits_the_best_likelihood_still_wins() -> None:
    top = _Candidate(score=-50.0, converged=False)
    best, _ = _select([_Candidate(-90.0, False), top])

    assert best is top
    assert best.monitor_.converged is False


def test_a_restart_is_never_scored_before_it_is_fitted() -> None:
    # Pins the ordering inside the guard: score() on an unfitted candidate
    # raises, so a reversed pair would show up as a failed seed.
    best, failed = _select([_Candidate(-1.0, True)])

    assert failed == []
    assert best is not None and best.fitted is True


def test_the_surviving_count_is_recoverable_from_the_failed_list() -> None:
    candidates = [
        _Candidate(-1.0, True),
        _Candidate(0.0, True, fit_raises=True),
        _Candidate(-2.0, True),
    ]
    _best, failed = _select(candidates)

    assert len(candidates) - len(failed) == 2
    assert failed == [1]


def test_scores_are_compared_not_assumed_positive() -> None:
    # HMM log-likelihoods are negative; a naive `if score > best_score` with
    # best_score initialised to 0.0 would reject every candidate.
    best, _ = _select([_Candidate(-1e9, True)])
    assert best is not None


def test_pytest_approx_is_not_needed_for_identity_selection() -> None:
    a = _Candidate(-10.0, True)
    b = _Candidate(-10.0, True)
    best, _ = _select([a, b])
    # Ties keep the incumbent — deterministic, lowest seed wins.
    assert best is a
    assert pytest.approx(best.score(None)) == -10.0
