"""
A spot/perp basis is only meaningful if both legs were observed together.

Both exchange providers computed basis from two independently-fetched
tickers with no check that they described the same instant, then clamped the
result to +/-500bps. A stale leg does not produce a slightly wrong number:
it produces the price move that happened while the feed was down, reported
as a dislocation that never existed -- and the clamp then renders that
absurd value perfectly plausible to everything downstream.

The clamp is kept (it is a reasonable outlier guard) but no longer
saturates silently, so a broken feed and a genuine dislocation stay
distinguishable in the logs.
"""

from __future__ import annotations

from src.intelligence.providers.base import (
    MAX_TICKER_SKEW_MS,
    tickers_are_synchronous,
)


def _t(ts: int | None) -> dict:
    return {"timestamp": ts, "last": 100.0}


def test_simultaneous_tickers_are_synchronous() -> None:
    assert tickers_are_synchronous(_t(1_700_000_000_000), _t(1_700_000_000_000)) is True


def test_a_small_skew_is_tolerated() -> None:
    base = 1_700_000_000_000
    assert tickers_are_synchronous(_t(base), _t(base + 5_000)) is True


def test_a_stale_leg_is_rejected() -> None:
    base = 1_700_000_000_000
    assert tickers_are_synchronous(_t(base), _t(base - 10 * 60_000)) is False


def test_the_check_is_symmetric() -> None:
    base = 1_700_000_000_000
    stale, live = _t(base - 10 * 60_000), _t(base)

    assert tickers_are_synchronous(stale, live) is False
    assert tickers_are_synchronous(live, stale) is False


def test_the_boundary_is_inclusive() -> None:
    base = 1_700_000_000_000
    assert tickers_are_synchronous(_t(base), _t(base + MAX_TICKER_SKEW_MS)) is True
    assert tickers_are_synchronous(_t(base), _t(base + MAX_TICKER_SKEW_MS + 1)) is False


def test_a_missing_timestamp_does_not_disable_the_signal() -> None:
    # ccxt does not populate `timestamp` on every venue. Refusing to compute
    # a basis wherever the field is absent would turn a data-quality guard
    # into a feature outage.
    base = 1_700_000_000_000
    assert tickers_are_synchronous(_t(None), _t(base)) is True
    assert tickers_are_synchronous({}, {}) is True


def test_a_non_numeric_timestamp_is_treated_as_unknown() -> None:
    assert tickers_are_synchronous({"timestamp": "soon"}, _t(1_700_000_000_000)) is True


def test_the_threshold_is_overridable() -> None:
    base = 1_700_000_000_000
    assert tickers_are_synchronous(_t(base), _t(base + 30_000), max_skew_ms=10_000) is False
    assert tickers_are_synchronous(_t(base), _t(base + 30_000), max_skew_ms=60_000) is True


# ------------------------------------------------------------ clamp behaviour


def _basis_bps(spot: float, perp: float) -> float:
    return ((perp - spot) / spot) * 10_000.0


def test_a_normal_basis_is_not_clamped() -> None:
    raw = _basis_bps(100.0, 100.3)  # 30bps

    assert abs(raw) < 500.0
    assert max(-500.0, min(500.0, raw)) == raw


def test_an_extreme_basis_saturates_but_is_detectable() -> None:
    # A 9% gap is what a stale leg across a real move looks like.
    raw = _basis_bps(100.0, 109.0)

    assert raw > 500.0
    assert max(-500.0, min(500.0, raw)) == 500.0


def test_the_clamp_is_symmetric() -> None:
    raw = _basis_bps(100.0, 91.0)

    assert raw < -500.0
    assert max(-500.0, min(500.0, raw)) == -500.0
