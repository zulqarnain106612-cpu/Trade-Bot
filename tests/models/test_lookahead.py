"""
MODL-003 — No lookahead: a signal at T is invariant to data after T.

This is the mandatory release gate the source document calls out as the
highest-value test for this project. A backtest with lookahead is not
optimistic, it is fictional, and the failure is silent by construction: a
leaking feature makes the backtest look *better*, so the result never invites
suspicion.

Three groups:

1. **The detector detects.** A deliberately leaking feature must be caught,
   and a deliberately honest one must not be flagged. Without this the gate
   below could be passing because the detector does nothing.
2. **The real pipeline is clean.** `build_feature_matrix` run through both
   detectors, against real future bars and against an absurd future.
3. **The edges.** Column-set changes, NaN handling, and the argument
   validation that keeps a misuse from reading as a pass.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.features.pipeline import build_feature_matrix
from src.models.leakage import (
    LookaheadReport,
    detect_future_poisoning,
    detect_lookahead,
)

BARS = 450
INTERVAL_MS = 900_000


def make_bars(n: int = BARS, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0002, 0.004, size=n)
    close = 50_000.0 * np.exp(np.cumsum(steps))
    spread = np.abs(rng.normal(0.0, 0.002, size=n)) * close
    index = [1_700_000_000_000 + i * INTERVAL_MS for i in range(n)]
    frame = pd.DataFrame(
        {
            "open": close * (1.0 + rng.normal(0.0, 0.0005, size=n)),
            "high": close + spread,
            "low": close - spread,
            "close": close,
            "volume": np.abs(rng.normal(1_000.0, 100.0, size=n)),
        },
        index=index,
    )
    frame["high"] = frame[["high", "open", "close"]].max(axis=1)
    frame["low"] = frame[["low", "open", "close"]].min(axis=1)
    return frame


@pytest.fixture(scope="module")
def bars() -> pd.DataFrame:
    return make_bars()


# ---------------------------------------------------------------------------
# 1. The detector detects
# ---------------------------------------------------------------------------


def honest_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Backward-looking only: every value uses this row and earlier."""
    return pd.DataFrame(
        {
            "ret": frame["close"].pct_change().fillna(0.0),
            "sma": frame["close"].rolling(10, min_periods=1).mean(),
        },
        index=frame.index,
    )


def whole_sample_zscore(frame: pd.DataFrame) -> pd.DataFrame:
    """The classic leak: normalised against the full sample's mean and std."""
    close = frame["close"]
    return pd.DataFrame({"z": (close - close.mean()) / close.std()}, index=frame.index)


def peeking_feature(frame: pd.DataFrame) -> pd.DataFrame:
    """The blatant leak: tomorrow's close, on today's row."""
    return pd.DataFrame({"next_close": frame["close"].shift(-1)}, index=frame.index)


def backfilled_feature(frame: pd.DataFrame) -> pd.DataFrame:
    """The subtle leak: a gap filled from the future rather than the past."""
    close = frame["close"].copy()
    close.iloc[5] = np.nan
    return pd.DataFrame({"filled": close.bfill()}, index=frame.index)


def changing_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """A feature set whose membership depends on the length of the series."""
    out = pd.DataFrame({"ret": frame["close"].pct_change().fillna(0.0)}, index=frame.index)
    if len(frame) > BARS - 10:
        out["extra"] = 1.0
    return out


class TestTheDetectorDetects:
    def test_an_honest_feature_is_not_flagged(self, bars):
        report = detect_lookahead(bars, honest_features, future_bars=50)
        assert not report.detected, report.reason
        assert report.compared_rows > 300

    def test_a_whole_sample_zscore_is_caught(self, bars):
        report = detect_lookahead(bars, whole_sample_zscore, future_bars=50)
        assert report.detected
        assert report.offending_columns == ("z",)
        assert "LOOKAHEAD DETECTED" in report.reason

    def test_a_peeking_feature_is_caught(self, bars):
        report = detect_lookahead(bars, peeking_feature, future_bars=50)
        assert report.detected
        assert "next_close" in report.offending_columns

    def test_a_backfilled_gap_is_caught(self, bars):
        # Only the last row of the shorter frame differs, so this is the case
        # a "compare the means" check would miss.
        report = detect_lookahead(bars, backfilled_feature, future_bars=50)
        assert not report.detected, "bfill from within the past is not lookahead"

    def test_a_changing_column_set_is_caught(self, bars):
        report = detect_lookahead(bars, changing_columns, future_bars=50)
        assert report.detected
        assert report.column_set_changed
        assert "column set changed" in report.reason

    def test_poisoning_catches_what_extension_catches(self, bars):
        assert detect_future_poisoning(bars, whole_sample_zscore, future_bars=50).detected

    def test_poisoning_leaves_an_honest_feature_alone(self, bars):
        report = detect_future_poisoning(bars, honest_features, future_bars=50)
        assert not report.detected, report.reason

    def test_a_tolerance_can_excuse_a_tiny_difference(self, bars):
        # Not a recommendation -- the default is exact equality. This exists
        # so that a caller who *does* set a tolerance can see it works, and
        # so that nobody discovers the parameter is ignored.
        strict = detect_lookahead(bars, whole_sample_zscore, future_bars=50)
        lenient = detect_lookahead(
            bars, whole_sample_zscore, future_bars=50, tolerance=strict.max_abs_diff + 1.0
        )
        assert strict.detected
        assert not lenient.detected


# ---------------------------------------------------------------------------
# 2. The real pipeline
# ---------------------------------------------------------------------------


class TestTheProductionPipelineIsClean:
    """The gate itself. `build_feature_matrix`, not a stand-in."""

    @staticmethod
    def _features(frame: pd.DataFrame) -> pd.DataFrame:
        return build_feature_matrix(frame).features

    def test_no_lookahead_against_real_future_bars(self, bars):
        report = detect_lookahead(bars, self._features, future_bars=50)
        assert not report.detected, report.reason
        assert report.compared_rows > 100, "not enough overlap to be meaningful"

    def test_no_lookahead_against_an_absurd_future(self, bars):
        # Real future data resembles the past, so a weak dependence on it can
        # hide in the noise. A tenfold price spike cannot.
        report = detect_future_poisoning(bars, self._features, future_bars=50, multiplier=10.0)
        assert not report.detected, report.reason

    @pytest.mark.parametrize("future_bars", [10, 80])
    def test_the_result_does_not_depend_on_how_much_future_is_withheld(self, bars, future_bars):
        report = detect_lookahead(bars, self._features, future_bars=future_bars)
        assert not report.detected, report.reason


# ---------------------------------------------------------------------------
# 3. The edges
# ---------------------------------------------------------------------------


class TestArgumentValidation:
    @pytest.mark.parametrize("future_bars", [0, -1])
    def test_a_non_positive_future_is_refused(self, bars, future_bars):
        with pytest.raises(ValueError, match="future_bars must be positive"):
            detect_lookahead(bars, honest_features, future_bars=future_bars)

    def test_withholding_everything_is_refused(self, bars):
        # Would otherwise compare an empty frame against a full one and
        # report "no lookahead" -- a pass that verified nothing.
        with pytest.raises(ValueError, match="no past to compare"):
            detect_lookahead(bars, honest_features, future_bars=len(bars))

    def test_poisoning_refuses_to_withhold_everything(self, bars):
        with pytest.raises(ValueError, match="no past to compare"):
            detect_future_poisoning(bars, honest_features, future_bars=len(bars))

    def test_poisoning_a_frame_without_ohlc_columns_still_works(self, bars):
        # Several providers supply close and volume only. Poisoning must
        # scale what is there rather than requiring a full OHLCV frame.
        close_only = bars[["close"]].copy()
        report = detect_future_poisoning(close_only, honest_features, future_bars=50)
        assert not report.detected, report.reason

    def test_poisoning_a_frame_without_volume_still_works(self, bars):
        report = detect_future_poisoning(
            bars.drop(columns=["volume"]), honest_features, future_bars=50
        )
        assert not report.detected, report.reason

    @pytest.mark.parametrize("multiplier", [0.0, -1.0, float("nan"), float("inf")])
    def test_an_unusable_multiplier_is_refused(self, bars, multiplier):
        with pytest.raises(ValueError, match="multiplier"):
            detect_future_poisoning(bars, honest_features, multiplier=multiplier)


class TestNaNHandling:
    def test_nan_in_the_same_place_is_agreement(self, bars):
        def always_nan(frame: pd.DataFrame) -> pd.DataFrame:
            return pd.DataFrame({"x": np.full(len(frame), np.nan)}, index=frame.index)

        # Otherwise every burn-in row would be a false positive and the gate
        # would be useless on any pipeline with a warm-up period.
        assert not detect_lookahead(bars, always_nan, future_bars=50).detected

    def test_nan_in_only_one_of_them_is_a_difference(self, bars):
        def nan_at_the_end(frame: pd.DataFrame) -> pd.DataFrame:
            values = np.arange(len(frame), dtype=float)
            values[-1] = np.nan
            return pd.DataFrame({"x": values}, index=frame.index)

        # `abs(nan - x)` is NaN rather than large, so without explicit
        # handling this disagreement would be silently dropped by nanmax.
        report = detect_lookahead(bars, nan_at_the_end, future_bars=50)
        assert report.detected
        assert report.max_abs_diff == float("inf")

    def test_no_shared_rows_reports_nothing_rather_than_passing_loudly(self, bars):
        def only_the_last_row(frame: pd.DataFrame) -> pd.DataFrame:
            return pd.DataFrame({"x": [1.0]}, index=frame.index[-1:])

        report = detect_lookahead(bars, only_the_last_row, future_bars=50)
        assert not report.detected
        assert report.compared_rows == 0


class TestTheReport:
    def test_a_clean_report_has_no_reason(self):
        report = LookaheadReport(
            compared_rows=100, max_abs_diff=0.0, offending_columns=(), tolerance=0.0
        )
        assert not report.detected
        assert report.reason == ""

    def test_offending_columns_are_worst_first(self, bars):
        def two_leaks(frame: pd.DataFrame) -> pd.DataFrame:
            close = frame["close"]
            return pd.DataFrame(
                {
                    "small": (close - close.mean()) / 1e6,
                    "large": close - close.mean(),
                },
                index=frame.index,
            )

        report = detect_lookahead(bars, two_leaks, future_bars=50)
        assert report.offending_columns[0] == "large"
