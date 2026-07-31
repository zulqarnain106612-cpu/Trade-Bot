"""Tests for src/execution/order_throttler.py"""

from __future__ import annotations

import time

import pytest

from src.execution.order_throttler import OrderThrottler, ThrottleResult


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


def test_init_zero_rate_raises():
    with pytest.raises(ValueError, match="rate"):
        OrderThrottler(rate=0.0, burst=10)


def test_init_negative_rate_raises():
    with pytest.raises(ValueError, match="rate"):
        OrderThrottler(rate=-1.0, burst=10)


def test_init_zero_burst_raises():
    with pytest.raises(ValueError, match="burst"):
        OrderThrottler(rate=10.0, burst=0)


def test_init_properties():
    t = OrderThrottler(rate=5.0, burst=15)
    assert t.rate == 5.0
    assert t.burst == 15


# ---------------------------------------------------------------------------
# acquire — basic contracts
# ---------------------------------------------------------------------------


def test_acquire_allowed_on_fresh_bucket():
    t = OrderThrottler(rate=10.0, burst=5)
    result = t.acquire("binance")
    assert result.allowed is True
    assert result.wait_s == 0.0
    assert result.reject_reason == ""


def test_acquire_returns_throttle_result():
    t = OrderThrottler(rate=10.0, burst=5)
    result = t.acquire("okx")
    assert isinstance(result, ThrottleResult)


def test_acquire_decrements_tokens():
    t = OrderThrottler(rate=10.0, burst=5)
    before = t.tokens_remaining("ex")
    t.acquire("ex")
    after = t.tokens_remaining("ex")
    assert after < before


def test_acquire_exhausts_bucket():
    t = OrderThrottler(rate=0.01, burst=3)  # very slow refill
    for _ in range(3):
        r = t.acquire("ex")
        assert r.allowed is True
    r = t.acquire("ex")
    assert r.allowed is False
    assert r.wait_s > 0


def test_acquire_rejected_has_reason():
    t = OrderThrottler(rate=0.001, burst=1)
    t.acquire("ex")  # drain
    result = t.acquire("ex")
    assert result.allowed is False
    assert "rate_limit" in result.reject_reason
    assert result.wait_s > 0


def test_acquire_result_frozen():
    t = OrderThrottler(rate=10.0, burst=5)
    r = t.acquire("ex")
    with pytest.raises((AttributeError, TypeError)):
        r.allowed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Per-exchange isolation
# ---------------------------------------------------------------------------


def test_separate_buckets_per_exchange():
    t = OrderThrottler(rate=0.001, burst=2)
    t.acquire("binance")
    t.acquire("binance")
    # binance exhausted; okx should still have tokens
    assert t.tokens_remaining("okx") == pytest.approx(2.0)
    r = t.acquire("okx")
    assert r.allowed is True


def test_n_exchanges_tracks_new_exchanges():
    t = OrderThrottler()
    assert t.n_exchanges == 0
    t.acquire("binance")
    assert t.n_exchanges == 1
    t.acquire("okx")
    assert t.n_exchanges == 2


# ---------------------------------------------------------------------------
# tokens_remaining
# ---------------------------------------------------------------------------


def test_tokens_remaining_full_on_first_access():
    t = OrderThrottler(rate=10.0, burst=10)
    assert t.tokens_remaining("new_exchange") == pytest.approx(10.0)


def test_tokens_remaining_decreases_after_acquire():
    t = OrderThrottler(rate=10.0, burst=10)
    t.acquire("ex")
    assert t.tokens_remaining("ex") == pytest.approx(9.0)


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def test_reset_single_exchange():
    t = OrderThrottler(rate=0.001, burst=3)
    t.acquire("ex")
    t.acquire("ex")
    t.reset("ex")
    assert t.tokens_remaining("ex") == pytest.approx(3.0)


def test_reset_all_exchanges():
    t = OrderThrottler(rate=0.001, burst=3)
    t.acquire("binance")
    t.acquire("okx")
    t.reset()
    assert t.tokens_remaining("binance") == pytest.approx(3.0)
    assert t.tokens_remaining("okx") == pytest.approx(3.0)


def test_reset_nonexistent_no_error():
    t = OrderThrottler()
    t.reset("ghost")  # should not raise


# ---------------------------------------------------------------------------
# set_rate
# ---------------------------------------------------------------------------


def test_set_rate_updates_rate():
    t = OrderThrottler(rate=5.0, burst=10)
    t.set_rate(20.0)
    assert t.rate == 20.0


def test_set_rate_zero_raises():
    t = OrderThrottler()
    with pytest.raises(ValueError):
        t.set_rate(0.0)


def test_set_rate_updates_burst():
    t = OrderThrottler(rate=5.0, burst=10)
    t.set_rate(5.0, burst=50)
    assert t.burst == 50


def test_set_rate_zero_burst_raises():
    t = OrderThrottler()
    with pytest.raises(ValueError):
        t.set_rate(5.0, burst=0)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_structure():
    t = OrderThrottler(rate=5.0, burst=10)
    t.acquire("binance")
    s = t.status()
    assert "rate" in s
    assert "burst" in s
    assert "exchanges" in s
    assert "binance" in s["exchanges"]


# ---------------------------------------------------------------------------
# Refill over time (smoke test — not timing-dependent)
# ---------------------------------------------------------------------------


def test_tokens_refill_after_drain():
    t = OrderThrottler(rate=1000.0, burst=2)  # very fast refill
    t.acquire("ex")
    t.acquire("ex")
    # Should be empty now
    assert t.tokens_remaining("ex") < 0.1
    time.sleep(0.002)  # 2ms → 2 tokens at 1000/s
    assert t.tokens_remaining("ex") > 0.5
