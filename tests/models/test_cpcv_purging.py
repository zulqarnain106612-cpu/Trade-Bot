"""
MODL-007 — cross-validation respects time and purging.

A leaky validation split reports skill the strategy does not have, and the leak
is invisible in the result: the score simply comes out higher. Two mechanisms
stop it, and both have to actually work:

- **Purging** removes training samples immediately *before* a test block,
  because a triple-barrier label at time T is decided by prices after T. A
  training sample whose label horizon overlaps the test window has seen the
  test window.
- **An embargo** removes training samples immediately *after* a test block,
  because serial correlation makes the bars just after the test window
  near-copies of its last bars.

The properties asserted here are the ones that decide whether a reported OOS
Sharpe means anything: no overlap, the gaps are actually applied, the fold
count is the combinatorial one, and a dataset too small to split is refused
rather than quietly producing folds that overlap.
"""

from __future__ import annotations

from itertools import combinations
from math import comb

import numpy as np
import pytest

from src.models.trainer import CPCVFold, build_cpcv_folds

N_SAMPLES = 1_000
N_SPLITS = 6
N_TEST_SPLITS = 2
PURGE_GAP = 10
EMBARGO_PCT = 0.01


@pytest.fixture
def folds() -> list[CPCVFold]:
    return build_cpcv_folds(
        n_samples=N_SAMPLES,
        n_splits=N_SPLITS,
        n_test_splits=N_TEST_SPLITS,
        purge_gap=PURGE_GAP,
        embargo_pct=EMBARGO_PCT,
    )


class TestTrainAndTestNeverOverlap:
    def test_no_sample_is_in_both(self, folds):
        for fold in folds:
            overlap = set(fold.train_idx) & set(fold.test_idx)
            assert not overlap, f"fold {fold.fold_id} has {len(overlap)} shared samples"

    def test_every_index_is_in_range(self, folds):
        for fold in folds:
            assert fold.train_idx.min() >= 0
            assert fold.test_idx.min() >= 0
            assert fold.train_idx.max() < N_SAMPLES
            assert fold.test_idx.max() < N_SAMPLES

    def test_no_index_appears_twice_within_a_split(self, folds):
        for fold in folds:
            assert len(set(fold.train_idx)) == len(fold.train_idx)
            assert len(set(fold.test_idx)) == len(fold.test_idx)


class TestPurging:
    def test_the_purge_gap_before_each_test_block_is_empty(self, folds):
        for fold in folds:
            test_start = int(fold.test_idx.min())
            forbidden = set(range(max(0, test_start - PURGE_GAP), test_start))
            intruders = forbidden & set(fold.train_idx)
            assert not intruders, (
                f"fold {fold.fold_id}: {len(intruders)} training samples inside the "
                f"{PURGE_GAP}-bar purge gap before test start {test_start}"
            )

    def test_a_larger_gap_removes_more(self):
        def train_size(gap: int) -> int:
            built = build_cpcv_folds(N_SAMPLES, N_SPLITS, 1, gap, 0.0)
            return sum(len(f.train_idx) for f in built)

        assert train_size(50) < train_size(5)

    def test_a_zero_gap_still_leaves_the_minimum_embargo(self):
        # `embargo_size = max(1, int(n * embargo_pct))`, so the embargo has a
        # floor of one bar and cannot be switched off. Pinned rather than
        # discovered: a reader who sets embargo_pct=0.0 and expects every
        # non-test sample back needs to know why one is missing.
        built = build_cpcv_folds(N_SAMPLES, N_SPLITS, 1, 0, 0.0)
        for fold in built:
            available = len(fold.train_idx) + len(fold.test_idx)
            assert available in (N_SAMPLES, N_SAMPLES - 1)


class TestEmbargo:
    def test_the_embargo_after_each_test_block_is_empty(self, folds):
        embargo_size = max(1, int(N_SAMPLES * EMBARGO_PCT))
        for fold in folds:
            test_end = int(fold.test_idx.max())
            forbidden = set(range(test_end + 1, min(N_SAMPLES, test_end + embargo_size + 1)))
            intruders = forbidden & set(fold.train_idx)
            assert not intruders, (
                f"fold {fold.fold_id}: {len(intruders)} training samples inside the "
                f"{embargo_size}-bar embargo after test end {test_end}"
            )

    def test_a_larger_embargo_removes_more(self):
        def train_size(pct: float) -> int:
            built = build_cpcv_folds(N_SAMPLES, N_SPLITS, 1, 0, pct)
            return sum(len(f.train_idx) for f in built)

        assert train_size(0.05) < train_size(0.001)


class TestTheCombinatorialStructure:
    def test_the_fold_count_is_the_binomial_coefficient(self, folds):
        # Every combination of test groups, not a single pass. That is what
        # makes it *combinatorial* purged CV rather than k-fold with a gap.
        assert len(folds) == comb(N_SPLITS, N_TEST_SPLITS)

    def test_every_combination_of_groups_appears_exactly_once(self, folds):
        assert len({fold.fold_id for fold in folds}) == len(folds)
        assert {f.fold_id for f in folds} == set(range(comb(N_SPLITS, N_TEST_SPLITS)))

    def test_each_test_set_is_the_union_of_whole_groups(self, folds):
        group_size = N_SAMPLES // N_SPLITS
        for fold in folds:
            assert len(fold.test_idx) % group_size == 0 or fold.test_idx.max() == N_SAMPLES - 1

    def test_test_indices_are_sorted(self, folds):
        for fold in folds:
            assert np.all(np.diff(fold.test_idx) > 0)

    def test_every_group_is_tested_by_some_fold(self, folds):
        tested = set()
        for fold in folds:
            tested.update(fold.test_idx.tolist())
        # Every sample appears in at least one test set, so no part of the
        # history is validated only in-sample.
        assert len(tested) == N_SAMPLES

    def test_the_number_of_folds_matches_the_enumeration(self):
        built = build_cpcv_folds(600, 4, 2, 5, 0.01)
        assert len(built) == len(list(combinations(range(4), 2)))


class TestGuards:
    @pytest.mark.parametrize("n_splits", [0, 1, -3])
    def test_too_few_splits_is_refused(self, n_splits):
        with pytest.raises(ValueError, match="n_splits must be >= 2"):
            build_cpcv_folds(100, n_splits, 1, 5, 0.01)

    def test_more_splits_than_samples_is_refused(self):
        # Would otherwise produce empty groups and fail deep in the call
        # stack with "zero-size array to reduction operation minimum" --
        # a cryptic crash on a new listing with too few bootstrapped bars.
        with pytest.raises(ValueError, match="every CPCV group must contain"):
            build_cpcv_folds(3, 6, 2, 5, 0.01)

    def test_a_dataset_too_small_for_meaningful_folds_yields_none(self):
        # Rather than folds with a handful of training rows, which would
        # produce a confident-looking score from nothing.
        assert build_cpcv_folds(40, 4, 2, 5, 0.01) == []

    def test_an_enormous_purge_gap_empties_everything_before_the_test_block(self):
        # Purging is directional and that is correct: it removes training
        # samples *before* the test window, because a label at time T is
        # decided by prices after T. Groups after the test window are the
        # embargo's job, so a huge purge gap does not empty them -- and a
        # test expecting no folds at all would be asserting the wrong model
        # of what purging does.
        built = build_cpcv_folds(300, 3, 1, 10_000, 0.0)
        assert built, "a large purge gap should not eliminate every fold"
        for fold in built:
            test_start = int(fold.test_idx.min())
            before = [i for i in fold.train_idx.tolist() if i < test_start]
            assert not before, f"fold {fold.fold_id} kept {len(before)} pre-test samples"
