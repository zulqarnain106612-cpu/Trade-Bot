"""
SIG-002 — Feature computation is deterministic and reproducible.

A backtest that cannot be reproduced is a result that cannot be trusted, and
non-determinism in a feature pipeline is the hardest kind to notice: the
numbers differ in the last few decimal places, the model retrains to slightly
different weights, and the strategy drifts without anything failing.

Three distinct properties, because they fail for different reasons:

1. **Same input, same output, same process.** Broken by an unseeded random
   draw, a `set` iteration order, or a dict ordering assumption.
2. **Same input, same output, different process.** Broken by hash
   randomisation (`PYTHONHASHSEED`), which is on by default and differs per
   interpreter.
3. **Prefix stability.** Computing features over bars 0..N and over
   bars 0..N+k must agree on the rows they share. Broken by any calculation
   that centres, normalises or fits over the whole series -- which is also
   how lookahead gets in, so this is the cheap early warning for `MODL-003`
   (PR-004).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.features.pipeline import bar_gap_report, build_feature_matrix

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Enough rows to clear the widest rolling window in the pipeline (the
#: 200-bar fractional-differentiation burn-in plus the triple-barrier
#: horizon) and still leave ~200 output rows for the prefix comparison.
#: Kept close to that floor on purpose: every row costs a fractional
#: differentiation and a rolling GARCH fit, and this module builds the matrix
#: a dozen times.
BARS = 450
INTERVAL_MS = 900_000


def make_bars(n: int = BARS, seed: int = 7) -> pd.DataFrame:
    """
    A deterministic pseudo-random OHLCV series.

    Seeded rather than smooth: a straight line makes every volatility feature
    zero, which would let a non-deterministic calculation agree with itself by
    accident.
    """
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 0.004, size=n)
    close = 50_000.0 * np.exp(np.cumsum(steps))
    spread = np.abs(rng.normal(0.0, 0.002, size=n)) * close
    index = [1_700_000_000_000 + i * INTERVAL_MS for i in range(n)]
    return pd.DataFrame(
        {
            "open": close * (1.0 + rng.normal(0.0, 0.0005, size=n)),
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": np.abs(rng.normal(1_000.0, 100.0, size=n)),
        },
        index=index,
    )


@pytest.fixture(scope="module")
def bars() -> pd.DataFrame:
    return make_bars()


class TestSameProcessDeterminism:
    def test_two_calls_produce_identical_features(self, bars):
        first = build_feature_matrix(bars)
        second = build_feature_matrix(bars)
        pd.testing.assert_frame_equal(first.features, second.features, check_exact=True)

    def test_the_labels_are_identical_too(self, bars):
        first = build_feature_matrix(bars)
        second = build_feature_matrix(bars)
        pd.testing.assert_series_equal(first.labels, second.labels, check_exact=True)

    def test_repeated_calls_agree_bit_for_bit(self, bars):
        # Once could be luck with a single-element cache. The pipeline runs
        # fractional differentiation and a rolling GARCH forecast over the
        # whole window, so this is a few seconds per call -- three is the
        # point where extra repetitions stop buying evidence and start
        # buying CI minutes.
        baseline = build_feature_matrix(bars).features.to_numpy()
        for _ in range(2):
            assert np.array_equal(
                build_feature_matrix(bars).features.to_numpy(), baseline, equal_nan=True
            )

    def test_the_input_frame_is_not_mutated(self, bars):
        # A pipeline that writes back into its input is reproducible only
        # until someone calls it twice on the same frame.
        before = bars.copy(deep=True)
        build_feature_matrix(bars)
        pd.testing.assert_frame_equal(bars, before, check_exact=True)


class TestCrossProcessDeterminism:
    def test_features_survive_a_different_hash_seed(self):
        """
        The property a same-process test cannot check.

        `PYTHONHASHSEED` is random per interpreter, so a pipeline that
        iterates a `set` of column names, or relies on any hash-ordered
        structure, produces a different column order -- or different values --
        in a fresh process. Two subprocesses with deliberately different
        seeds is the only way to see it.
        """
        script = "\n".join(
            (
                f"import sys; sys.path.insert(0, {str(PROJECT_ROOT)!r})",
                "import numpy as np",
                "from tests.features.test_feature_determinism import make_bars",
                "from src.features.pipeline import build_feature_matrix",
                "fm = build_feature_matrix(make_bars())",
                "arr = fm.features.to_numpy()",
                # Marker-prefixed, because the pipeline logs timestamped lines
                # to stdout and two subprocesses never share a timestamp.
                "print('RESULT', list(fm.features.columns))",
                "print('RESULT', repr(float(np.nansum(arr))),"
                " repr(float(np.nanstd(arr))), arr.shape)",
            )
        )

        outputs = []
        for seed in ("0", "12345"):
            proc = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                cwd=PROJECT_ROOT,
                timeout=600,
                env={
                    "PYTHONHASHSEED": seed,
                    "PATH": "/usr/bin:/bin:/usr/local/bin",
                    "HOME": str(PROJECT_ROOT),
                },
                check=False,
            )
            assert proc.returncode == 0, proc.stderr[-2000:]
            outputs.append([ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")])
            assert outputs[-1], f"subprocess produced no result lines: {proc.stdout[-2000:]}"

        assert outputs[0] == outputs[1], (
            "feature pipeline is sensitive to PYTHONHASHSEED:\n"
            f"seed 0:     {outputs[0]}\nseed 12345: {outputs[1]}"
        )


class TestPrefixStability:
    """
    Features at time T must not change when later bars arrive.

    This is a weaker statement than the lookahead gate that lands in PR-004
    (`MODL-003`), and it is the cheap version of the same question: any
    calculation that normalises over the whole series fails here first.

    Labels are deliberately *excluded*. Triple-barrier labelling looks forward
    by construction -- that is what a label is -- so a label near the end of a
    truncated series legitimately changes when the future arrives. Conflating
    the two is how a real lookahead bug gets excused as "that is just the
    labels".
    """

    def test_shared_rows_agree_when_more_bars_arrive(self, bars):
        short = build_feature_matrix(bars.iloc[:-50]).features
        long = build_feature_matrix(bars).features

        shared = short.index.intersection(long.index)
        assert len(shared) > 100, "not enough overlap to be a meaningful check"

        pd.testing.assert_frame_equal(
            short.loc[shared], long.loc[shared][short.columns], check_exact=False, atol=0, rtol=0
        )

    def test_the_column_set_does_not_depend_on_the_series_length(self, bars):
        short = build_feature_matrix(bars.iloc[:-50]).features
        long = build_feature_matrix(bars).features
        assert list(short.columns) == list(long.columns)


class TestGapReportingIsDeterministic:
    def test_the_same_index_reports_the_same_gaps(self, bars):
        assert bar_gap_report(bars.index) == bar_gap_report(bars.index)

    def test_a_contiguous_index_reports_no_gaps(self, bars):
        report = bar_gap_report(bars.index)
        assert report["gap_count"] == 0
        assert report["expected_ms"] == INTERVAL_MS

    def test_a_missing_run_is_counted(self, bars):
        gapped = bars.drop(index=bars.index[100:105])
        report = bar_gap_report(gapped.index)
        assert report["gap_count"] == 1
        assert report["missing_bars"] == 5

    @pytest.mark.parametrize("n", [0, 1, 2])
    def test_too_few_points_have_no_inferable_spacing(self, bars, n):
        assert bar_gap_report(bars.index[:n])["expected_ms"] == 0


class TestTheInputContract:
    @pytest.mark.parametrize("column", ["open", "high", "low", "close", "volume"])
    def test_a_missing_column_raises_rather_than_producing_features(self, bars, column):
        with pytest.raises(ValueError, match="missing required columns"):
            build_feature_matrix(bars.drop(columns=[column]))

    def test_too_few_bars_raises_rather_than_producing_short_windows(self, bars):
        with pytest.raises(ValueError, match="need at least"):
            build_feature_matrix(bars.iloc[:50])
