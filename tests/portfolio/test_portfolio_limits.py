"""
PORT-001 — Portfolio-level exposure, correlation and agreement limits are
enforced before an order is sized, not reported after the fact.

Five uncorrelated-looking positions can be one position in five costumes. The
two controls that stop that are both *scalars* rather than vetoes, which is a
deliberate design choice -- a veto belongs to the risk gates, which can
actually block -- and it makes them easy to get wrong in a specific way: a
scalar that is computed, logged and then not multiplied into anything looks
exactly like a working control.

So each property here is asserted on the number that reaches the sizer, not on
the number the tracker reports.
"""

from __future__ import annotations

import math

import pytest

from src.risk.portfolio_agreement import portfolio_agreement_scalar
from src.risk.portfolio_correlation import (
    PortfolioCorrelationTracker,
    get_portfolio_correlation,
)
from src.strategies.position_sizing import correlation_adjusted_notional


def _feed(tracker: PortfolioCorrelationTracker, series: dict[str, list[float]]) -> None:
    """Push aligned per-bar returns for every symbol."""
    length = len(next(iter(series.values())))
    for i in range(length):
        tracker.push_bar_returns({sym: values[i] for sym, values in series.items()})


def _identical(n: int) -> list[float]:
    # Deterministic, non-degenerate, and perfectly reproducible.
    return [math.sin(i / 3.0) / 100.0 for i in range(n)]


#: Sample-size shrinkage, from src/risk/portfolio_correlation.
#: `r_reported = r_raw * n / (n + K)`. A correlation estimated from 31 bars is
#: noisy and should not drive as large a size reduction as one estimated from
#: thousands, so the estimator shrinks toward independence and the shrinkage
#: vanishes as n grows. Every expected value below is written through this
#: function rather than as a bare number, so a change to K is a one-line
#: change here instead of a scatter of mysterious constants.
SHRINKAGE_K = 20.0


def _shrunk(raw: float, n: int) -> float:
    return raw * n / (n + SHRINKAGE_K)


class TestCorrelationIsMeasured:
    def test_identical_series_correlate_at_one_before_shrinkage(self):
        tracker = PortfolioCorrelationTracker()
        moves = _identical(200)
        _feed(tracker, {"BTC/USDT": moves, "ETH/USDT": list(moves)})
        assert tracker.correlation("BTC/USDT", "ETH/USDT") == pytest.approx(
            _shrunk(1.0, 200), abs=1e-6
        )

    def test_shrinkage_vanishes_as_the_sample_grows(self):
        # The property that makes the shrinkage defensible: it is a
        # small-sample correction, not a permanent discount on the estimate.
        short, long = PortfolioCorrelationTracker(), PortfolioCorrelationTracker()
        _feed(short, {"A": _identical(40), "B": _identical(40)})
        _feed(long, {"A": _identical(2_000), "B": _identical(2_000)})
        assert short.correlation("A", "B") < long.correlation("A", "B") < 1.0
        assert long.correlation("A", "B") == pytest.approx(_shrunk(1.0, 2_000), abs=1e-6)

    def test_opposite_series_correlate_at_minus_one_before_shrinkage(self):
        tracker = PortfolioCorrelationTracker()
        moves = _identical(200)
        _feed(tracker, {"BTC/USDT": moves, "ETH/USDT": [-m for m in moves]})
        assert tracker.correlation("BTC/USDT", "ETH/USDT") == pytest.approx(
            _shrunk(-1.0, 200), abs=1e-6
        )

    def test_too_little_history_reports_nothing_rather_than_guessing(self):
        tracker = PortfolioCorrelationTracker()
        _feed(tracker, {"BTC/USDT": [0.01, 0.02], "ETH/USDT": [0.01, 0.02]})
        assert tracker.correlation("BTC/USDT", "ETH/USDT") is None

    def test_an_unknown_pair_reports_nothing(self):
        tracker = PortfolioCorrelationTracker()
        assert tracker.correlation("BTC/USDT", "DOGE/USDT") is None

    def test_a_symbol_correlates_with_itself(self):
        tracker = PortfolioCorrelationTracker()
        _feed(tracker, {"BTC/USDT": _identical(200)})
        assert tracker.correlation("BTC/USDT", "BTC/USDT") == pytest.approx(1.0, abs=1e-6)


class TestTheAverageAgainstAnOpenBook:
    def test_an_empty_book_is_uncorrelated(self):
        tracker = PortfolioCorrelationTracker()
        assert tracker.avg_correlation_with_open_positions("BTC/USDT", []) == 0.0

    def test_a_book_with_no_history_is_treated_as_independent(self):
        # Documented as conservative-for-sizing rather than conservative-for-
        # risk: no data means full size. Pinned so the choice stays visible.
        tracker = PortfolioCorrelationTracker()
        assert tracker.avg_correlation_with_open_positions("BTC/USDT", ["ETH/USDT"]) == 0.0

    def test_a_perfectly_correlated_book_averages_to_one(self):
        tracker = PortfolioCorrelationTracker()
        moves = _identical(200)
        _feed(
            tracker,
            {"BTC/USDT": moves, "ETH/USDT": list(moves), "SOL/USDT": list(moves)},
        )
        avg = tracker.avg_correlation_with_open_positions("BTC/USDT", ["ETH/USDT", "SOL/USDT"])
        assert avg == pytest.approx(_shrunk(1.0, 200), abs=1e-6)

    def test_negative_correlations_are_clamped_to_zero(self):
        # A hedge does not increase Kelly sizing in this model; that is a
        # separate decision, and letting it through as a negative average
        # would inflate the position rather than merely not shrinking it.
        tracker = PortfolioCorrelationTracker()
        moves = _identical(200)
        _feed(tracker, {"BTC/USDT": moves, "ETH/USDT": [-m for m in moves]})
        assert tracker.avg_correlation_with_open_positions("BTC/USDT", ["ETH/USDT"]) == 0.0

    def test_the_average_is_always_a_correlation(self):
        tracker = PortfolioCorrelationTracker()
        moves = _identical(200)
        _feed(
            tracker,
            {
                "BTC/USDT": moves,
                "ETH/USDT": [m * 0.5 for m in moves],
                "SOL/USDT": [-m for m in moves],
            },
        )
        avg = tracker.avg_correlation_with_open_positions("BTC/USDT", ["ETH/USDT", "SOL/USDT"])
        assert 0.0 <= avg <= 1.0


class TestTheScalarActuallyReducesThePosition:
    def test_below_the_threshold_nothing_is_reduced(self):
        tracker = PortfolioCorrelationTracker()
        assert tracker.correlation_scalar("BTC/USDT", []) == 1.0

    def test_a_perfectly_correlated_book_is_cut_hard(self):
        tracker = PortfolioCorrelationTracker()
        moves = _identical(200)
        _feed(tracker, {"BTC/USDT": moves, "ETH/USDT": list(moves)})
        # Linear from 1.0 at the 0.60 threshold to 0.0 at correlation 1.0,
        # applied to the shrunk estimate.
        expected = (1.0 - _shrunk(1.0, 200)) / (1.0 - 0.60)
        assert tracker.correlation_scalar("BTC/USDT", ["ETH/USDT"]) == pytest.approx(
            round(expected, 4)
        )

    def test_with_enough_history_a_duplicate_position_is_nearly_eliminated(self):
        # The operationally important case: shrinkage is what keeps a
        # 40-bar estimate from halving a position, and it must not keep a
        # 2000-bar estimate from doing its job.
        tracker = PortfolioCorrelationTracker()
        moves = _identical(2_000)
        _feed(tracker, {"BTC/USDT": moves, "ETH/USDT": list(moves)})
        assert tracker.correlation_scalar("BTC/USDT", ["ETH/USDT"]) < 0.03

    def test_the_scalar_is_always_in_the_unit_interval(self):
        tracker = PortfolioCorrelationTracker()
        moves = _identical(200)
        _feed(
            tracker,
            {
                "BTC/USDT": moves,
                "ETH/USDT": [m * 0.9 + 0.0001 for m in moves],
                "SOL/USDT": [m * 0.2 for m in moves],
            },
        )
        for book in ([], ["ETH/USDT"], ["SOL/USDT"], ["ETH/USDT", "SOL/USDT"]):
            scalar = tracker.correlation_scalar("BTC/USDT", book)
            assert 0.0 <= scalar <= 1.0

    def test_the_haircut_binds_on_the_notional_that_reaches_the_sizer(self):
        # The end-to-end shape PORT-001 is actually about: a measured
        # correlation must arrive as a smaller notional, not as a log line.
        tracker = PortfolioCorrelationTracker()
        moves = _identical(2_000)
        _feed(tracker, {"BTC/USDT": moves, "ETH/USDT": list(moves)})
        avg = tracker.avg_correlation_with_open_positions("BTC/USDT", ["ETH/USDT"])
        reduced = correlation_adjusted_notional(50_000.0, avg)
        assert reduced < 50_000.0 * 0.05, f"duplicate position only cut to {reduced}"

    def test_an_uncorrelated_book_leaves_the_notional_alone(self):
        assert correlation_adjusted_notional(50_000.0, 0.0) == 50_000.0

    @pytest.mark.parametrize(
        ("correlation", "expected"),
        [(0.7, 1.0), (0.775, 0.75), (0.85, 0.5), (0.925, 0.25), (1.0, 0.0)],
    )
    def test_the_reduction_is_linear_from_the_threshold_to_one(self, correlation, expected):
        assert correlation_adjusted_notional(1_000.0, correlation) == pytest.approx(
            1_000.0 * expected
        )


class TestPortfolioAgreement:
    class _Resolution:
        agreement_ratio = 0.4

    class _Evaluation:
        def __init__(self, direction: int, conflict: bool, voters: int) -> None:
            self.direction = direction
            self.conflict = conflict
            self.voting_ids = tuple(f"family_{i}" for i in range(voters))
            self.resolution = TestPortfolioAgreement._Resolution()

    def test_no_evaluation_means_no_reduction(self):
        assert portfolio_agreement_scalar(None, 1) == 1.0

    def test_a_flat_trade_has_no_direction_to_disagree_with(self):
        assert portfolio_agreement_scalar(self._Evaluation(-1, False, 5), 0) == 1.0

    def test_one_voter_is_an_opinion_not_a_consensus(self):
        # Also covers the unwired-feed case: abstention must not read as
        # dissent, or an integration gap silently halves every position.
        assert portfolio_agreement_scalar(self._Evaluation(-1, False, 1), 1) == 1.0

    def test_agreement_does_not_reduce(self):
        assert portfolio_agreement_scalar(self._Evaluation(1, False, 5), 1) == 1.0

    def test_opposition_reduces_hardest(self):
        opposed = portfolio_agreement_scalar(self._Evaluation(-1, False, 5), 1)
        conflicted = portfolio_agreement_scalar(self._Evaluation(0, True, 5), 1)
        assert 0.0 < opposed < conflicted < 1.0

    def test_a_scalar_never_vetoes(self):
        # A floor well above zero is deliberate: the incumbent family is the
        # only one with out-of-sample validation, so the rest of the book may
        # shrink the trade but never block it. Blocking belongs to the gates.
        for evaluation in (
            self._Evaluation(-1, False, 5),
            self._Evaluation(0, True, 5),
            self._Evaluation(-1, True, 12),
        ):
            assert portfolio_agreement_scalar(evaluation, 1) > 0.0

    def test_a_portfolio_with_no_direction_of_its_own_does_not_oppose(self):
        assert portfolio_agreement_scalar(self._Evaluation(0, False, 5), 1) == 1.0


class TestTheSingleton:
    def test_it_is_stable(self):
        assert get_portfolio_correlation() is get_portfolio_correlation()
