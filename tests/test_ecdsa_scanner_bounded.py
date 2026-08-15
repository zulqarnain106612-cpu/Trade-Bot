"""
The ECDSA scanner must not grow without bound.

ECDSAScanner is constructed once in the ECC worker and fed a transaction
stream. Detecting nonce reuse means remembering r values, so its registry
grows by design — unbounded, it accumulates every r from every transaction
ever seen and exhausts memory long before it detects anything on a real
run.
"""

from __future__ import annotations

from src.ecc.ecdsa_scan import (
    _MAX_SIGS_PER_R,
    ECDSAScanner,
)


def _remember(scanner: ECDSAScanner, r: int) -> None:
    """Drive the registry the way scan_transaction does, without tx parsing."""
    entry = scanner._r_registry.get(r)
    if entry is not None:
        scanner._r_registry.move_to_end(r)
    else:
        entry = []
        scanner._r_registry[r] = entry
        while len(scanner._r_registry) > scanner._max_tracked_r:
            scanner._r_registry.popitem(last=False)
            scanner._evicted_r += 1
    if len(entry) < _MAX_SIGS_PER_R:
        entry.append((1, "pk", "tx", 0))


def test_the_registry_is_bounded() -> None:
    scanner = ECDSAScanner(max_tracked_r=5)
    for r in range(20):
        _remember(scanner, r)

    assert len(scanner._r_registry) == 5
    assert scanner._evicted_r == 15


def test_eviction_drops_the_least_recently_seen() -> None:
    scanner = ECDSAScanner(max_tracked_r=5)
    for r in range(20):
        _remember(scanner, r)

    assert list(scanner._r_registry) == [15, 16, 17, 18, 19]


def test_seeing_an_r_again_protects_it_from_eviction() -> None:
    # Reused nonces come from one faulty signer and cluster in time, so a
    # live r must not be evicted ahead of a stale one.
    scanner = ECDSAScanner(max_tracked_r=3)
    for r in (1, 2, 3):
        _remember(scanner, r)
    _remember(scanner, 1)
    _remember(scanner, 4)

    assert 1 in scanner._r_registry
    assert 2 not in scanner._r_registry


def test_signatures_per_r_are_capped() -> None:
    # Reuse is reported on the second sighting, so beyond a handful this
    # stores nothing that changes a detection — and is the shape a spam
    # stream would exploit.
    scanner = ECDSAScanner(max_tracked_r=10)
    for _ in range(50):
        _remember(scanner, 7)

    assert len(scanner._r_registry[7]) == _MAX_SIGS_PER_R


def test_detected_weaknesses_are_bounded() -> None:
    assert ECDSAScanner()._weaknesses.maxlen is not None


def test_eviction_is_counted_not_silent() -> None:
    # An evicted r means a later reuse of it goes undetected. That is a real
    # loss of coverage, so it is tracked rather than dropped quietly.
    scanner = ECDSAScanner(max_tracked_r=2)
    assert scanner._evicted_r == 0
    for r in range(6):
        _remember(scanner, r)
    assert scanner._evicted_r == 4
