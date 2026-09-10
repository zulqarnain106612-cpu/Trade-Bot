"""
Tests for :func:`src.security.constant_time.timing_leak_test`.

The roadmap wires this probe into the constant_time suite and requires it to
pass on the current implementation: the ``hmac.compare_digest``-backed
comparators must show no data-dependent timing gap. Because wall-clock timing
is noisy, the *detector's own logic* -- that it fires on an early-exit
comparator and stays quiet on a constant-time one -- is pinned deterministically
with an injected clock, and the real-clock check on the shipped functions uses
the module's own threshold with a loose margin.
"""

from __future__ import annotations

import pytest

from src.security.constant_time import (
    TimingReport,
    safe_compare,
    safe_compare_bytes,
    safe_compare_tokens,
    timing_leak_test,
)


def test_the_comparators_agree_with_equality() -> None:
    """The shipped constant-time comparators return the same verdict as ==."""
    assert safe_compare("token-abc", "token-abc")
    assert not safe_compare("token-abc", "token-xyz")
    assert safe_compare_bytes(b"\x01\x02", b"\x01\x02")
    assert not safe_compare_bytes(b"\x01\x02", b"\x01\x03")
    assert safe_compare_tokens("key-123", "key-123")
    assert not safe_compare_tokens("key-123", "key-124")

# ---- deterministic detector logic (injected clock) -------------------------


def _leaky_clock():
    """A fake clock plus an early-exit comparator that advances it by the
    number of matching prefix bytes -- the timing signature of a real leak,
    made exact so the detector's verdict is deterministic."""
    clock = [0]

    def timer() -> int:
        return clock[0]

    def leaky(guess: bytes, secret: bytes) -> bool:
        matched = 0
        for x, y in zip(guess, secret, strict=False):
            if x != y:
                break
            matched += 1
        clock[0] += matched + 1  # consume time proportional to prefix
        return guess == secret

    return timer, leaky


def test_the_detector_fires_on_an_early_exit_comparator() -> None:
    timer, leaky = _leaky_clock()
    report = timing_leak_test(leaky, length=32, trials=50, timer=timer)
    assert report.leak_detected
    assert report.late_median_ns > report.early_median_ns


def test_the_detector_stays_quiet_on_a_constant_time_comparator() -> None:
    """A comparator that always scans the whole input, under the same injected
    clock, advances by a fixed amount regardless of the input -- no gap."""
    clock = [0]

    def timer() -> int:
        return clock[0]

    def constant(guess: bytes, secret: bytes) -> bool:
        acc = 0
        for x, y in zip(guess, secret, strict=False):
            acc |= x ^ y
        clock[0] += len(secret)  # fixed cost, independent of the data
        return acc == 0

    report = timing_leak_test(constant, length=32, trials=50, timer=timer)
    assert not report.leak_detected
    assert report.ratio == pytest.approx(0.0)


# ---- the shipped comparators, on the real clock ----------------------------


def test_safe_compare_bytes_shows_no_timing_leak() -> None:
    """The exit-gate assertion: the constant-time function passes its own probe."""
    report = timing_leak_test(safe_compare_bytes, length=32, trials=3000)
    assert not report.leak_detected


def test_the_report_carries_the_measured_medians() -> None:
    report = timing_leak_test(safe_compare_bytes, length=16, trials=200)
    assert isinstance(report, TimingReport)
    assert report.early_median_ns > 0
    assert report.late_median_ns > 0
    assert report.ratio >= 0


# ---- validation ------------------------------------------------------------


def test_length_below_two_is_refused() -> None:
    with pytest.raises(ValueError, match="length"):
        timing_leak_test(safe_compare_bytes, length=1)


def test_zero_trials_is_refused() -> None:
    with pytest.raises(ValueError, match="trials"):
        timing_leak_test(safe_compare_bytes, trials=0)
