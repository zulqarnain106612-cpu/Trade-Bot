"""
VEN-001 -- per-venue connect, disconnect and account verification.

Two lights per venue, decided separately: market data (did the venue's public
markets load) and account access (did one authenticated, read-only
fetch_balance succeed). Disconnecting one venue closes exactly its client,
leaves the other alone, is refused for the last venue standing, and is never
reported as healthy afterwards. Concurrent reconnects are serialised, so two
clicks cannot leak a client. Every exchange here is a fake: no credentials,
no network, no orders.

Decides:
  - VEN-001 -- venue connections are controlled per venue, with honest state
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import ccxt.async_support as ccxt
import pytest

from src.config import EXCHANGE_BINANCE, EXCHANGE_OKX
from src.data import fetcher as fetcher_module
from src.data.fetcher import MarketDataFetcher, VenueDisconnectRefused, _describe_error

SIGNED = "AuthenticationError: binance {\"code\":-2015} apiKey=" + "a" * 64


class FakeExchange:
    loading = 0
    max_loading = 0

    def __init__(
        self, error: Exception | None = None, balance=None, gate: asyncio.Event | None = None
    ):
        self._error = error
        self._balance = balance
        self._gate = gate
        self.closed = 0
        self.balance_calls = 0
        self.close_error: Exception | None = None

    async def load_markets(self) -> dict:
        FakeExchange.loading += 1
        FakeExchange.max_loading = max(FakeExchange.max_loading, FakeExchange.loading)
        try:
            if self._gate is not None:
                await self._gate.wait()
            if self._error is not None:
                raise self._error
            return {"BTC/USDT": {}}
        finally:
            FakeExchange.loading -= 1

    async def fetch_balance(self) -> dict:
        self.balance_calls += 1
        if isinstance(self._balance, Exception):
            raise self._balance
        if self._balance == "hang":
            await asyncio.Event().wait()
        return {"total": {"USDT": 1.0}}

    async def close(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def _settings(binance=("key", "secret"), okx=("", "", "")):
    return SimpleNamespace(
        binance=SimpleNamespace(api_key=binance[0], api_secret=binance[1], testnet=True),
        okx=SimpleNamespace(api_key=okx[0], api_secret=okx[1], passphrase=okx[2], testnet=False),
    )


@pytest.fixture
def venues(monkeypatch):
    queue = {EXCHANGE_BINANCE: [], EXCHANGE_OKX: []}
    built = {EXCHANGE_BINANCE: [], EXCHANGE_OKX: []}

    def take(venue):
        exchange = queue[venue].pop(0) if queue[venue] else FakeExchange()
        built[venue].append(exchange)
        return exchange

    monkeypatch.setattr("src.data.fetcher._build_binance", lambda cfg: take(EXCHANGE_BINANCE))
    monkeypatch.setattr("src.data.fetcher._build_okx", lambda cfg: take(EXCHANGE_OKX))

    async def once(coro_factory, label, *args, **kwargs):
        return await coro_factory()

    monkeypatch.setattr("src.data.fetcher._with_retry", once)
    FakeExchange.loading = FakeExchange.max_loading = 0
    instance = MarketDataFetcher(storage=object())
    instance._settings = _settings()
    instance.queue, instance.built = queue, built  # type: ignore[attr-defined]
    return instance


class TestReportedState:
    def test_before_initialize_nothing_is_claimed(self, venues):
        status = venues.venue_status()
        for venue in (EXCHANGE_BINANCE, EXCHANGE_OKX):
            assert status[venue]["state"] == "unavailable"
            assert status[venue]["available"] is False
            unchecked = {"state": "unavailable", "error": None, "checked_at": None}
            assert status[venue]["account"] == unchecked
        assert status[EXCHANGE_BINANCE]["credentials"] == "configured"
        assert status[EXCHANGE_OKX]["credentials"] == "missing"
        assert status[EXCHANGE_BINANCE]["testnet"] is True
        assert status[EXCHANGE_OKX]["testnet"] is False

    async def test_market_data_up_does_not_mean_the_account_works(self, venues):
        await venues.initialize()
        status = venues.venue_status()
        assert status[EXCHANGE_BINANCE]["state"] == "connected"
        assert status[EXCHANGE_BINANCE]["account"]["state"] == "unverified"
        assert status[EXCHANGE_OKX]["state"] == "connected"
        assert status[EXCHANGE_OKX]["account"]["state"] == "unconfigured"
        assert status[EXCHANGE_BINANCE]["changed_at"].endswith("+00:00")

    async def test_a_venue_that_did_not_load_is_failed_with_its_reason(self, venues):
        venues.queue[EXCHANGE_OKX].append(FakeExchange(ccxt.ExchangeNotAvailable("okx 503")))
        await venues.initialize()
        okx = venues.venue_status()[EXCHANGE_OKX]
        assert (okx["state"], okx["available"]) == ("failed", False)
        assert "503" in okx["error"]

    def test_a_stale_connected_phase_is_never_reported(self, venues):
        venues._venue_state[EXCHANGE_OKX].phase = "connected"
        assert venues.venue_status()[EXCHANGE_OKX]["state"] == "unavailable"
        venues._venue_errors[EXCHANGE_OKX] = "boom"
        assert venues.venue_status()[EXCHANGE_OKX]["state"] == "failed"

    @pytest.mark.parametrize(
        ("binance", "okx", "expected"),
        [
            (("k", "s"), ("k", "s", "p"), ("configured", "configured")),
            (("k", ""), ("k", "s", ""), ("incomplete", "incomplete")),
            (("", " "), ("", "", ""), ("missing", "missing")),
        ],
    )
    def test_credentials_are_described_never_shown(self, venues, binance, okx, expected):
        venues._settings = _settings(binance, okx)
        states = (venues.credential_state(EXCHANGE_BINANCE), venues.credential_state(EXCHANGE_OKX))
        assert states == expected


class TestDisconnect:
    async def test_disconnecting_one_venue_leaves_the_other_alone(self, venues):
        await venues.initialize()
        okx_client = venues.built[EXCHANGE_OKX][0]
        binance_client = venues.built[EXCHANGE_BINANCE][0]

        await venues.disconnect(EXCHANGE_OKX)

        assert okx_client.closed == 1 and binance_client.closed == 0
        assert venues.available_venues() == {EXCHANGE_BINANCE}
        okx = venues.venue_status()[EXCHANGE_OKX]
        assert okx["state"] == "disconnected"
        assert okx["operator_disconnected"] is True
        assert okx["error"] is None
        assert okx["account"]["state"] == "unavailable"
        with pytest.raises(RuntimeError, match="disconnected by an operator"):
            venues._require_okx()
        assert venues.get_order_exchange() is binance_client

    async def test_the_last_venue_cannot_be_disconnected(self, venues):
        await venues.initialize()
        await venues.disconnect(EXCHANGE_OKX)
        with pytest.raises(VenueDisconnectRefused):
            await venues.disconnect(EXCHANGE_BINANCE)
        assert venues.available_venues() == {EXCHANGE_BINANCE}

    async def test_disconnecting_twice_is_harmless(self, venues):
        await venues.initialize()
        await venues.disconnect(EXCHANGE_OKX)
        await venues.disconnect(EXCHANGE_OKX)
        assert venues.built[EXCHANGE_OKX][0].closed == 1

    async def test_a_client_that_fails_to_close_is_still_dropped(self, venues):
        await venues.initialize()
        venues.built[EXCHANGE_OKX][0].close_error = OSError("socket already gone")
        await venues.disconnect(EXCHANGE_OKX)
        assert EXCHANGE_OKX not in venues.available_venues()

    async def test_reconnect_brings_a_disconnected_venue_back(self, venues):
        await venues.initialize()
        await venues.disconnect(EXCHANGE_OKX)
        assert await venues.reconnect(EXCHANGE_OKX) is True
        okx = venues.venue_status()[EXCHANGE_OKX]
        assert (okx["state"], okx["operator_disconnected"]) == ("connected", False)
        assert len(venues.built[EXCHANGE_OKX]) == 2

    async def test_unknown_venues_are_rejected(self, venues):
        for call in (venues.disconnect, venues.verify_account, venues.reconnect):
            with pytest.raises(ValueError, match="unknown venue"):
                await call("kraken")


class TestReconnectInProgress:
    async def test_the_phase_shows_the_attempt_and_attempts_do_not_overlap(self, venues):
        await venues.initialize()
        gate = asyncio.Event()
        venues.queue[EXCHANGE_OKX].extend([FakeExchange(gate=gate), FakeExchange(gate=gate)])
        first = asyncio.ensure_future(venues.reconnect(EXCHANGE_OKX))
        second = asyncio.ensure_future(venues.reconnect(EXCHANGE_OKX))
        await asyncio.sleep(0)
        assert venues.venue_status()[EXCHANGE_OKX]["state"] == "reconnecting"
        gate.set()
        assert await asyncio.gather(first, second) == [True, True]
        assert FakeExchange.max_loading == 1
        # Each replaced client was closed; only the newest is live.
        clients = venues.built[EXCHANGE_OKX]
        assert [c.closed for c in clients] == [1, 1, 0]
        assert venues._okx is clients[-1]

    async def test_a_first_attempt_reports_connecting(self, venues):
        gate = asyncio.Event()
        venues.queue[EXCHANGE_OKX].append(FakeExchange(gate=gate))
        attempt = asyncio.ensure_future(venues.reconnect(EXCHANGE_OKX))
        await asyncio.sleep(0)
        assert venues.venue_status()[EXCHANGE_OKX]["state"] == "connecting"
        gate.set()
        assert await attempt is True


class TestAccountVerification:
    async def test_a_successful_read_only_call_authenticates(self, venues):
        await venues.initialize()
        assert await venues.verify_account(EXCHANGE_BINANCE) == "authenticated"
        account = venues.venue_status()[EXCHANGE_BINANCE]["account"]
        assert account["state"] == "authenticated" and account["checked_at"]
        assert venues.built[EXCHANGE_BINANCE][0].balance_calls == 1

    async def test_rejected_credentials_are_reported_without_the_key(self, venues):
        rejecting = FakeExchange(balance=ccxt.AuthenticationError(SIGNED))
        venues.queue[EXCHANGE_BINANCE].append(rejecting)
        await venues.initialize()
        assert await venues.verify_account(EXCHANGE_BINANCE) == "rejected"
        error = venues.venue_status()[EXCHANGE_BINANCE]["account"]["error"]
        assert "AuthenticationError" in error and "a" * 64 not in error

    @pytest.mark.parametrize("problem", [ccxt.NetworkError("timeout"), "hang"])
    async def test_an_incomplete_check_is_failed_not_rejected(self, venues, monkeypatch, problem):
        monkeypatch.setattr(fetcher_module, "_ACCOUNT_CHECK_TIMEOUT_S", 0.01)
        venues.queue[EXCHANGE_BINANCE].append(FakeExchange(balance=problem))
        await venues.initialize()
        assert await venues.verify_account(EXCHANGE_BINANCE) == "failed"

    async def test_without_full_credentials_nothing_is_attempted(self, venues):
        await venues.initialize()
        assert await venues.verify_account(EXCHANGE_OKX) == "unconfigured"
        account = venues.venue_status()[EXCHANGE_OKX]["account"]
        assert account["error"] == "no API credentials are configured"
        venues._settings = _settings(okx=("k", "s", ""))
        assert await venues.verify_account(EXCHANGE_OKX) == "unconfigured"
        account = venues.venue_status()[EXCHANGE_OKX]["account"]
        assert account["error"] == "credentials are incomplete"
        assert venues.built[EXCHANGE_OKX][0].balance_calls == 0

    async def test_a_venue_that_is_not_connected_cannot_be_verified(self, venues):
        assert await venues.verify_account(EXCHANGE_BINANCE) == "unavailable"


def test_error_text_is_bounded_and_redacted():
    text = _describe_error(ValueError("GET /api?signature=" + "f" * 64 + " " + "x" * 500))
    assert "f" * 64 not in text
    assert len(text) <= 300
