"""
Constant-time comparison utilities (Part II §9.4).

All secret comparisons must use these functions — not `==`.
Prevents Kocher (1996) timing attack: no branch on secret bytes,
no early-exit loops over secret data.

post_quantum posture (LAW12):
  Nothing here needs a PQC migration, and the reason is not "it is only a
  comparison". This module contains no asymmetric cryptography at all --
  hmac.compare_digest is a byte-wise equality check, so there is no
  discrete-log or factoring problem for Shor's algorithm to solve.

  Grover's algorithm is the only quantum result that touches this code
  path, and it applies to the secrets being compared rather than to the
  comparison: it halves the effective search space, leaving a 256-bit token
  at ~128 bits, which is the accepted post-quantum floor. The requirement
  that follows is on the callers -- keep compared secrets at 256 bits --
  not on this file.

  The gate flags it because its scope is src/security/, not because it has
  quantum-fragile primitives.
"""

from __future__ import annotations

import hmac
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass


def safe_compare(a: str, b: str) -> bool:
    """Constant-time string comparison via HMAC-digest."""
    return hmac.compare_digest(a.encode(), b.encode())


def safe_compare_bytes(a: bytes, b: bytes) -> bool:
    """Constant-time bytes comparison."""
    return hmac.compare_digest(a, b)


def safe_compare_tokens(provided: str, stored: str) -> bool:
    """API key / auth token validation — always constant-time."""
    return hmac.compare_digest(
        provided.encode("utf-8"),
        stored.encode("utf-8"),
    )


@dataclass(frozen=True)
class TimingReport:
    """
    The result of a timing-leak probe on a byte comparator.

    ``early_median_ns`` times inputs that differ from the secret in their first
    byte; ``late_median_ns`` times inputs that match every byte but the last. A
    comparator that early-exits on the first mismatch finishes the first class
    fast and the second class slow, so a large gap between the two medians is
    the signature of a data-dependent branch. ``ratio`` normalises that gap, and
    ``leak_detected`` is the verdict against the threshold.
    """

    early_median_ns: float
    late_median_ns: float
    ratio: float
    leak_detected: bool


def timing_leak_test(
    compare_fn: Callable[[bytes, bytes], bool],
    *,
    length: int = 32,
    trials: int = 2000,
    threshold: float = 0.25,
    timer: Callable[[], int] = time.perf_counter_ns,
) -> TimingReport:
    """
    Probe ``compare_fn`` for a data-dependent timing difference.

    The test contrasts two input classes against a fixed secret: guesses that
    differ in the *first* byte and guesses that differ only in the *last*. A
    constant-time comparator spends the same time on both -- it never short-
    circuits -- so their median timings are within noise. A naive ``==`` or a
    hand-rolled loop that returns on the first mismatch is markedly faster on
    the first class, and ``leak_detected`` fires.

    ``ratio`` is ``|late - early| / min(early, late)``; ``leak_detected`` is
    ``ratio > threshold``. Wall-clock timing is noisy, so the medians are taken
    over ``trials`` interleaved measurements after a warm-up, and the default
    threshold is deliberately loose -- a real early-exit leak on 32 bytes is a
    far larger gap than any jitter this introduces. ``timer`` is injectable so
    the detector's own logic can be tested deterministically rather than
    against a real clock.

    This is a diagnostic, not a defence: a clean report is evidence, not proof,
    that a comparator is constant-time (it cannot see cache or microarchitectural
    leakage), which is why the module's real defence is delegating to
    ``hmac.compare_digest`` rather than trusting this test.
    """
    if length < 2:
        raise ValueError("length must be at least 2 to place a differing last byte")
    if trials < 1:
        raise ValueError("trials must be at least 1")

    secret = bytes([0xA5]) * length
    differ_first = bytes([0x00]) + secret[1:]
    differ_last = secret[:-1] + bytes([0x00])

    # Warm up so the first measurements do not pay one-off import/JIT costs.
    for _ in range(min(trials, 200)):
        compare_fn(differ_first, secret)
        compare_fn(differ_last, secret)

    early: list[int] = []
    late: list[int] = []
    for _ in range(trials):
        start = timer()
        compare_fn(differ_first, secret)
        early.append(timer() - start)
        start = timer()
        compare_fn(differ_last, secret)
        late.append(timer() - start)

    early_median = statistics.median(early)
    late_median = statistics.median(late)
    denom = min(early_median, late_median) or 1
    ratio = abs(late_median - early_median) / denom
    return TimingReport(
        early_median_ns=early_median,
        late_median_ns=late_median,
        ratio=ratio,
        leak_detected=ratio > threshold,
    )
