"""Coverage for the stale-ticker basis suppression in the three exchange providers.

binance/okx/bybit each carry an identical guard: when the spot and perp
tickers were observed too far apart, the basis is suppressed to 0.0
rather than reporting the move the stale leg missed as a dislocation.
That branch was uncovered in all three.

No test opens a ccxt connection -- both exchange handles are AsyncMocks.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.intelligence.providers.base import MAX_TICKER_SKEW_MS
from src.intelligence.providers.binance_provider import BinanceIntelligenceProvider
from src.intelligence.providers.bybit_provider import BybitIntelligenceProvider
from src.intelligence.providers.okx_provider import OKXIntelligenceProvider


def _wire(provider, spot_ticker: dict, perp_ticker: dict):
    """Attach fake spot/perp exchange handles and bypass the response cache."""
    provider._spot = MagicMock()
    provider._spot.fetch_ticker = AsyncMock(return_value=spot_ticker)
    provider._perp = MagicMock()
    provider._perp.fetch_ticker = AsyncMock(return_value=perp_ticker)
    provider._get_cache = MagicMock(return_value=None)
    return provider


_PROVIDERS = [BinanceIntelligenceProvider, BybitIntelligenceProvider, OKXIntelligenceProvider]


@pytest.mark.parametrize("provider_cls", _PROVIDERS)
async def test_basis_suppressed_when_tickers_out_of_sync(provider_cls):
    skew = MAX_TICKER_SKEW_MS + 5_000
    provider = _wire(
        provider_cls(),
        spot_ticker={"last": 100.0, "timestamp": 1_000_000},
        perp_ticker={"last": 110.0, "timestamp": 1_000_000 + skew},
    )
    # A 10% gap would read as +1000bps (clamped to 500) if the stale leg were
    # trusted; the guard must return exactly 0.0 instead.
    assert await provider._fetch_basis_data() == 0.0


@pytest.mark.parametrize("provider_cls", _PROVIDERS)
async def test_basis_computed_when_tickers_synchronous(provider_cls):
    provider = _wire(
        provider_cls(),
        spot_ticker={"last": 100.0, "timestamp": 1_000_000},
        perp_ticker={"last": 101.0, "timestamp": 1_000_000 + 10},
    )
    assert await provider._fetch_basis_data() == pytest.approx(100.0)


@pytest.mark.parametrize("provider_cls", _PROVIDERS)
async def test_basis_zero_when_spot_price_missing(provider_cls):
    provider = _wire(
        provider_cls(),
        spot_ticker={"last": None, "close": None},
        perp_ticker={"last": 101.0},
    )
    assert await provider._fetch_basis_data() == 0.0


@pytest.mark.parametrize("provider_cls", _PROVIDERS)
async def test_basis_zero_when_spot_price_non_positive(provider_cls):
    provider = _wire(
        provider_cls(),
        spot_ticker={"last": 0.0, "close": 0.0},
        perp_ticker={"last": 101.0},
    )
    assert await provider._fetch_basis_data() == 0.0


@pytest.mark.parametrize("provider_cls", _PROVIDERS)
async def test_basis_clamped_to_500_bps(provider_cls):
    provider = _wire(
        provider_cls(),
        spot_ticker={"last": 100.0, "timestamp": 1_000},
        perp_ticker={"last": 300.0, "timestamp": 1_000},
    )
    # Raw basis is +20_000bps; the clamp caps it at the ±500 bound.
    assert await provider._fetch_basis_data() == pytest.approx(500.0)


@pytest.mark.parametrize("provider_cls", _PROVIDERS)
async def test_basis_uses_close_when_last_absent(provider_cls):
    provider = _wire(
        provider_cls(),
        spot_ticker={"close": 100.0, "timestamp": 1_000},
        perp_ticker={"close": 101.0, "timestamp": 1_000},
    )
    assert await provider._fetch_basis_data() == pytest.approx(100.0)


@pytest.mark.parametrize("provider_cls", _PROVIDERS)
async def test_basis_computed_when_timestamps_absent(provider_cls):
    # A missing timestamp must not disable the signal -- the helper documents
    # that it returns True (synchronous) when it cannot check.
    provider = _wire(
        provider_cls(),
        spot_ticker={"last": 100.0},
        perp_ticker={"last": 101.0},
    )
    assert await provider._fetch_basis_data() == pytest.approx(100.0)
