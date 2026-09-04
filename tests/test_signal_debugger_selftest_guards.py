"""The pipeline selftest must still fail on bad data under `python -O`.

Its three checks used to be bare asserts. `-O` strips asserts, so an
optimised run would skip every check and report passed=True on a broken
pipeline -- a selftest that cannot fail is worse than no selftest, because
something downstream trusts its verdict.

These tests do not run under -O (pytest does not), so they cannot observe
the stripping directly. What they pin is the property that makes the
stripping irrelevant: each condition raises through a normal code path
that -O does not touch, and the selftest reports the failure rather than
swallowing it.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.diagnostics.signal_debugger import run_pipeline_selftest


class _FakeMatrix:
    def __init__(self, features) -> None:
        self.features = features


def _run_with_matrix(features) -> dict:
    with patch(
        "src.features.pipeline.build_feature_matrix",
        return_value=_FakeMatrix(features),
    ):
        return run_pipeline_selftest()


def test_selftest_fails_when_the_feature_matrix_is_none() -> None:
    result = _run_with_matrix(None)

    assert result["passed"] is False
    assert "feature matrix is None" in result["error"]


def test_selftest_fails_when_the_feature_matrix_is_empty() -> None:
    pd = pytest.importorskip("pandas")

    result = _run_with_matrix(pd.DataFrame())

    assert result["passed"] is False
    assert "feature matrix empty" in result["error"]


def test_the_real_pipeline_trips_none_of_the_three_guards() -> None:
    """Guards against the checks being trivially unreachable.

    Asserting passed=True here would couple this test to every optional
    dependency build_feature_matrix reaches for. What matters is narrower:
    on the real pipeline none of the three conditions fires, so they are
    guards against genuine breakage rather than a branch nothing enters.
    """
    pytest.importorskip("pandas")

    result = run_pipeline_selftest()

    error = result["error"] or ""
    for message in ("feature matrix is None", "feature matrix empty", "NaN in features"):
        assert message not in error, f"the real pipeline tripped {message!r}"
