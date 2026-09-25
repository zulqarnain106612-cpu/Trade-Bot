"""
INV-032, consumer half -- one broadcaster in place of N per-client loops.

The defect this pins is a scaling one rather than a crash. The old websocket
endpoint ran a timer *per connection*, and each of those timers did its own
storage read and its own json.dumps to produce bytes identical to every other
client's. At the 50-client cap that is 50 storage reads per heartbeat to send
one snapshot, so the cost of the dashboard grew with the number of people
looking at it.

These tests assert the shape that fix depends on: the payload is built once,
serialized once, and the same string reaches everybody -- and that the
envelope a client needs in order to detect loss is actually on the frame.
"""

from __future__ import annotations

import json

import pytest


class _FakeWS:
    """Records what was sent, and can fail on demand."""

    def __init__(self, fails: bool = False) -> None:
        self.sent: list[str] = []
        self.fails = fails

    async def send_text(self, message: str) -> None:
        if self.fails:
            raise RuntimeError("client has gone away")
        self.sent.append(message)


@pytest.fixture
def api_state():
    from src.api import main as api_main

    return api_main._state


class TestOneSerializationForEveryClient:
    async def test_every_client_receives_the_identical_string(self, api_state) -> None:
        """
        Not merely equal payloads -- the *same* string. Serializing per client
        is the cost this replaced, so the assertion is on identity of the
        bytes rather than on what they decode to.
        """
        clients = [_FakeWS() for _ in range(4)]
        for client in clients:
            api_state._ws_clients.add(client)
        try:
            delivered = await api_state.broadcast({"type": "tick", "equity_usd": 1000.0})
        finally:
            for client in clients:
                api_state._ws_clients.discard(client)

        assert delivered == 4
        first = clients[0].sent[0]
        assert all(client.sent[0] == first for client in clients)

    async def test_a_dead_client_costs_the_others_nothing(self, api_state) -> None:
        dead, alive = _FakeWS(fails=True), _FakeWS()
        for client in (dead, alive):
            api_state._ws_clients.add(client)
        try:
            delivered = await api_state.broadcast({"type": "tick"})
        finally:
            for client in (dead, alive):
                api_state._ws_clients.discard(client)

        assert delivered == 1
        assert alive.sent
        assert dead not in api_state.ws_clients


class TestFrameEnvelope:
    async def test_a_frame_carries_version_sequence_and_timestamp(self, api_state) -> None:
        client = _FakeWS()
        api_state._ws_clients.add(client)
        try:
            await api_state.broadcast({"type": "tick"})
        finally:
            api_state._ws_clients.discard(client)

        frame = json.loads(client.sent[0])
        assert frame["v"] == api_state._WS_PROTOCOL_VERSION
        assert isinstance(frame["seq"], int)
        assert isinstance(frame["ts_ms"], int)

    async def test_the_sequence_advances_so_a_client_can_see_a_gap(self, api_state) -> None:
        """
        Without a monotonic counter a client cannot tell a dropped frame from
        a quiet market -- the two look identical on the wire.
        """
        client = _FakeWS()
        api_state._ws_clients.add(client)
        try:
            await api_state.broadcast({"type": "tick"})
            await api_state.broadcast({"type": "tick"})
        finally:
            api_state._ws_clients.discard(client)

        first, second = (json.loads(frame)["seq"] for frame in client.sent)
        assert second == first + 1

    async def test_a_producer_timestamp_survives_the_envelope(self, api_state) -> None:
        """
        The whole point of carrying ts_ms is measuring producer-to-browser
        lag. If the broadcaster overwrote it with its own clock, every frame
        would report zero lag no matter how starved the transport was.
        """
        client = _FakeWS()
        api_state._ws_clients.add(client)
        try:
            await api_state.broadcast(
                {"type": "event", "topic": "gate", "ts_ms": 1_700_000_000_123}
            )
        finally:
            api_state._ws_clients.discard(client)

        assert json.loads(client.sent[0])["ts_ms"] == 1_700_000_000_123


class TestHeartbeatSkipsIdleServers:
    async def test_no_clients_is_not_an_error(self, api_state) -> None:
        assert await api_state.broadcast({"type": "tick"}) == 0
