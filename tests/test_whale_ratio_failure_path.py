"""
Tests for the whale-ratio failure path in BinanceIntelligenceProvider.

_fetch_whale_taker_ratio caught its own exception and returned the neutral
1.0. fetch_metrics() gathers it with return_exceptions=True and has a branch
that logs whale_failed and applies a confidence penalty — but that branch was
unreachable, because the result was always a float. A failed fetch was
indistinguishable from genuinely balanced taker flow, and check_whale_activity
draws exactly that distinction on purpose.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.intelligence.providers.binance_provider import BinanceIntelligenceProvider


def _provider() -> BinanceIntelligenceProvider:
    provider = object.__new__(BinanceIntelligenceProvider)
    provider._perp = AsyncMock()
    provider._perp_symbol = "BTC/USDT:USDT"
    provider._kline_tf = "1h"
    provider._cache = {}
    provider._cache_ttl = 300.0
    import structlog

    provider._log = structlog.get_logger().bind(component="test")
    return provider


async def test_a_kline_failure_propagates_rather_than_faking_neutral() -> None:
    # The caller's failure branch can only run if the exception reaches it.
    provider = _provider()
    provider._perp.market = lambda _s: {"id": "BTCUSDT"}
    provider._perp.fapiPublicGetKlines = AsyncMock(side_effect=RuntimeError("503"))

    with pytest.raises(RuntimeError, match="503"):
        await provider._fetch_whale_taker_ratio()


async def test_a_failed_fetch_is_not_cached() -> None:
    # Caching the fabricated neutral served it for the whole TTL, so one
    # transient failure suppressed the gate's real input for minutes.
    provider = _provider()
    provider._perp.market = lambda _s: {"id": "BTCUSDT"}
    provider._perp.fapiPublicGetKlines = AsyncMock(side_effect=RuntimeError("503"))

    with pytest.raises(RuntimeError):
        await provider._fetch_whale_taker_ratio()

    assert provider._cache == {}


async def test_an_empty_window_is_still_neutral_and_cached() -> None:
    # An empty kline response is not an error and not evidence of imbalance;
    # it is a real, stable state, so the neutral value is legitimate here.
    provider = _provider()
    provider._perp.market = lambda _s: {"id": "BTCUSDT"}
    provider._perp.fapiPublicGetKlines = AsyncMock(return_value=[])

    assert await provider._fetch_whale_taker_ratio() == 1.0
    assert provider._cache != {}


async def test_a_successful_fetch_is_cached() -> None:
    provider = _provider()
    provider._perp.market = lambda _s: {"id": "BTCUSDT"}
    provider._perp.fapiPublicGetKlines = AsyncMock(
        return_value=[[0, "1", "1", "1", "1", "1000", 0, "1000", "1", "600", "600", "0"]]
    )

    ratio = await provider._fetch_whale_taker_ratio()
    assert ratio > 1.0  # 600 taker-buy of 1000 total -> buy-side dominant
    assert provider._cache != {}
