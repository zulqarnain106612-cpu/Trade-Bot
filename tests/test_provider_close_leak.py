"""
Provider shutdown must close every session, even when one close fails.

close() gathers the spot and perp ccxt sessions. Without return_exceptions
the first failure cancels the sibling coroutine, so its aiohttp session is
never closed and leaks for the life of the process — on the shutdown path,
where nothing downstream will ever retry it.
"""

from __future__ import annotations

import pytest

from src.intelligence.providers.binance_provider import BinanceIntelligenceProvider
from src.intelligence.providers.bybit_provider import BybitIntelligenceProvider
from src.intelligence.providers.okx_provider import OKXIntelligenceProvider


class _Session:
    def __init__(self, *, fails: bool = False) -> None:
        self.closed = False
        self._fails = fails

    async def close(self) -> None:
        if self._fails:
            raise RuntimeError("transport already torn down")
        self.closed = True


PROVIDERS = [
    BinanceIntelligenceProvider,
    BybitIntelligenceProvider,
    OKXIntelligenceProvider,
]


@pytest.mark.parametrize("provider_cls", PROVIDERS)
async def test_a_failing_spot_close_still_closes_the_perp_session(provider_cls) -> None:
    provider = object.__new__(provider_cls)
    provider._spot = _Session(fails=True)
    provider._perp = _Session()

    await provider.close()

    assert provider._perp.closed is True


@pytest.mark.parametrize("provider_cls", PROVIDERS)
async def test_a_failing_perp_close_still_closes_the_spot_session(provider_cls) -> None:
    provider = object.__new__(provider_cls)
    provider._spot = _Session()
    provider._perp = _Session(fails=True)

    await provider.close()

    assert provider._spot.closed is True


@pytest.mark.parametrize("provider_cls", PROVIDERS)
async def test_close_does_not_raise_when_both_sessions_fail(provider_cls) -> None:
    # Shutdown must not raise: there is nothing left to recover to.
    provider = object.__new__(provider_cls)
    provider._spot = _Session(fails=True)
    provider._perp = _Session(fails=True)

    await provider.close()


@pytest.mark.parametrize("provider_cls", PROVIDERS)
async def test_the_healthy_path_closes_both(provider_cls) -> None:
    provider = object.__new__(provider_cls)
    provider._spot = _Session()
    provider._perp = _Session()

    await provider.close()

    assert provider._spot.closed and provider._perp.closed
