"""
One venue being unreachable must not take the bot down.

REG-0014. `initialize()` loaded both venues in a single sequence with no
isolation, so any failure on the first aborted startup entirely: an outage, a
rate limit, or a jurisdictional block answering 451 on Binance stopped OKX
strategies that never needed Binance at all. Observed against a real 451 --
"Service unavailable from a restricted location" -- which made the backend
unstartable from an affected network however healthy everything else was.

The contract these tests hold:
  * a venue that fails is recorded unavailable, and the other still comes up
  * only a total loss raises, because a fetcher with no venue has nothing to do
  * an unavailable venue's accessor says why, not "not initialized"
  * reconnect() brings a venue back in place, with no restart
"""

from __future__ import annotations

import ccxt.async_support as ccxt
import pytest

from src.config import EXCHANGE_BINANCE, EXCHANGE_OKX
from src.data.fetcher import MarketDataFetcher

GEO_BLOCK = "binance GET https://api.binance.com/api/v3/exchangeInfo 451 Unknown"


class FakeExchange:
    """A ccxt stand-in whose load_markets outcome the test decides."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.load_calls = 0
        self.closed = 0

    async def load_markets(self) -> dict:
        self.load_calls += 1
        if self._error is not None:
            raise self._error
        return {"BTC/USDT": {}}

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture
def fetcher(monkeypatch):
    """
    A fetcher whose venue builders are swapped for fakes.

    `builders` maps a venue to the FakeExchange handed out next, so a test
    decides each venue's fate independently and can hand out a *different*
    exchange on a later reconnect.
    """
    built: dict[str, list[FakeExchange]] = {EXCHANGE_BINANCE: [], EXCHANGE_OKX: []}
    queue: dict[str, list[FakeExchange]] = {EXCHANGE_BINANCE: [], EXCHANGE_OKX: []}

    def _take(venue: str) -> FakeExchange:
        exchange = queue[venue].pop(0) if queue[venue] else FakeExchange()
        built[venue].append(exchange)
        return exchange

    monkeypatch.setattr("src.data.fetcher._build_binance", lambda cfg: _take(EXCHANGE_BINANCE))
    monkeypatch.setattr("src.data.fetcher._build_okx", lambda cfg: _take(EXCHANGE_OKX))

    # _with_retry binds `attempts=_RETRY_ATTEMPTS` as a default argument, which
    # is evaluated at import, so patching the module constant does not reach it
    # and every failure path really sleeps through the backoff. The retry
    # behaviour has its own tests; what these need is one attempt and no clock.
    async def _once(coro_factory, label, *args, **kwargs):
        return await coro_factory()

    monkeypatch.setattr("src.data.fetcher._with_retry", _once)

    instance = MarketDataFetcher(storage=object())
    instance._test_queue = queue  # type: ignore[attr-defined]
    instance._test_built = built  # type: ignore[attr-defined]
    return instance


class TestOneVenueDownDoesNotStopTheOther:
    @pytest.mark.parametrize(
        "down, up",
        [(EXCHANGE_BINANCE, EXCHANGE_OKX), (EXCHANGE_OKX, EXCHANGE_BINANCE)],
    )
    async def test_the_healthy_venue_still_comes_up(self, fetcher, down, up):
        fetcher._test_queue[down].append(FakeExchange(ccxt.ExchangeNotAvailable(GEO_BLOCK)))

        await fetcher.initialize()

        assert fetcher.available_venues() == {up}
        assert fetcher.venue_status()[up]["available"] is True
        assert fetcher.venue_status()[down]["available"] is False

    async def test_the_failure_reason_is_kept_not_discarded(self, fetcher):
        fetcher._test_queue[EXCHANGE_BINANCE].append(
            FakeExchange(ccxt.ExchangeNotAvailable(GEO_BLOCK))
        )

        await fetcher.initialize()

        error = fetcher.venue_status()[EXCHANGE_BINANCE]["error"]
        # An operator has to tell a 451 from a network failure; "unavailable"
        # does not, so the venue's own words are carried through.
        assert "451" in error
        assert "ExchangeNotAvailable" in error

    async def test_a_failed_venue_is_closed_not_leaked(self, fetcher):
        broken = FakeExchange(ccxt.ExchangeNotAvailable(GEO_BLOCK))
        fetcher._test_queue[EXCHANGE_BINANCE].append(broken)

        await fetcher.initialize()

        assert broken.closed == 1
        assert fetcher._binance is None


class TestTotalLossStillRaises:
    async def test_no_venue_at_all_is_an_error(self, fetcher):
        for venue in (EXCHANGE_BINANCE, EXCHANGE_OKX):
            fetcher._test_queue[venue].append(FakeExchange(ccxt.ExchangeNotAvailable("down")))

        with pytest.raises(RuntimeError, match="no exchange venue could be initialized"):
            await fetcher.initialize()

    async def test_the_error_names_every_venue_and_its_reason(self, fetcher):
        fetcher._test_queue[EXCHANGE_BINANCE].append(FakeExchange(ccxt.ExchangeNotAvailable("451")))
        fetcher._test_queue[EXCHANGE_OKX].append(FakeExchange(ccxt.ExchangeNotAvailable("timeout")))

        with pytest.raises(RuntimeError) as exc_info:
            await fetcher.initialize()

        message = str(exc_info.value)
        assert EXCHANGE_BINANCE in message and "451" in message
        assert EXCHANGE_OKX in message and "timeout" in message


class TestTheAccessorSaysWhy:
    async def test_an_unavailable_venue_names_its_reason_and_the_way_back(self, fetcher):
        fetcher._test_queue[EXCHANGE_BINANCE].append(
            FakeExchange(ccxt.ExchangeNotAvailable(GEO_BLOCK))
        )
        await fetcher.initialize()

        with pytest.raises(RuntimeError) as exc_info:
            fetcher.get_order_exchange()

        message = str(exc_info.value)
        assert "451" in message
        assert "reconnect" in message
        assert "not initialized" not in message

    def test_a_fetcher_nobody_initialized_still_says_so(self, fetcher):
        with pytest.raises(RuntimeError, match="not initialized"):
            fetcher.get_order_exchange()


class TestReconnectNeedsNoRestart:
    async def test_a_dead_venue_comes_back_in_place(self, fetcher):
        fetcher._test_queue[EXCHANGE_BINANCE].append(
            FakeExchange(ccxt.ExchangeNotAvailable(GEO_BLOCK))
        )
        await fetcher.initialize()
        assert fetcher.available_venues() == {EXCHANGE_OKX}

        # The venue answers this time.
        fetcher._test_queue[EXCHANGE_BINANCE].append(FakeExchange())
        assert await fetcher.reconnect(EXCHANGE_BINANCE) is True

        assert fetcher.available_venues() == {EXCHANGE_BINANCE, EXCHANGE_OKX}
        assert fetcher.venue_status()[EXCHANGE_BINANCE]["error"] is None

    async def test_reconnecting_a_live_venue_closes_the_old_client(self, fetcher):
        await fetcher.initialize()
        first = fetcher._test_built[EXCHANGE_OKX][0]

        fetcher._test_queue[EXCHANGE_OKX].append(FakeExchange())
        assert await fetcher.reconnect(EXCHANGE_OKX) is True

        assert first.closed == 1, "the replaced client must not be leaked"
        assert fetcher._okx is not first

    async def test_a_reconnect_that_fails_leaves_the_venue_down_not_half_open(self, fetcher):
        await fetcher.initialize()

        fetcher._test_queue[EXCHANGE_OKX].append(FakeExchange(ccxt.ExchangeNotAvailable("still")))
        assert await fetcher.reconnect(EXCHANGE_OKX) is False

        assert fetcher._okx is None
        assert fetcher.venue_status()[EXCHANGE_OKX]["available"] is False

    async def test_an_unknown_venue_is_rejected(self, fetcher):
        with pytest.raises(ValueError, match="unknown venue"):
            await fetcher.reconnect("kraken")
