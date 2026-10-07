"""
Topic subscriptions, and the authorization that goes with them.

Two separate properties share this file because they share a mechanism:

  SEC   a read-only key must not receive the approval queue. An approval
        frame carries a pending trade -- symbol, direction, size -- awaiting
        an operator decision. A read-only key exists so something can watch
        without being able to act; handing it the decisions being made is
        neither read-only in spirit nor needed by anything it renders.

  INV   filtering must not reintroduce per-client serialization. The whole
        reason the per-connection loops were deleted is that they did one
        json.dumps per client for identical bytes. A subscription model that
        builds a payload per recipient brings that straight back, so the
        filter selects recipients and the serialization still happens once.
"""

from __future__ import annotations

import json

import pytest

from src.api.access_control import Role
from src.eventbus import TOPICS


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, message: str) -> None:
        self.sent.append(message)


@pytest.fixture
def api_state():
    from src.api import main as api_main

    state = api_main._state
    yield state
    state._ws_clients.clear()
    state._ws_topics.clear()
    state._ws_roles.clear()


class TestRoleAuthorization:
    def test_a_readonly_key_is_denied_the_approval_topic(self, api_state) -> None:
        permitted = api_state.permitted_topics(Role.READ_ONLY)

        assert "approval" not in permitted
        assert "equity" in permitted  # everything else still flows

    def test_a_trade_authorizing_key_gets_everything(self, api_state) -> None:
        assert api_state.permitted_topics(Role.TRADE_AUTHORIZING) == TOPICS

    async def test_a_readonly_socket_never_receives_an_approval_frame(self, api_state) -> None:
        """
        The end-to-end version: not merely that the topic set excludes it,
        but that a published approval does not reach the socket.
        """
        readonly, operator = _FakeWS(), _FakeWS()
        api_state._ws_clients.update({readonly, operator})
        await api_state.set_ws_topics(readonly, api_state.permitted_topics(Role.READ_ONLY))
        await api_state.set_ws_topics(operator, api_state.permitted_topics(Role.TRADE_AUTHORIZING))

        delivered = await api_state.broadcast(
            {"type": "event", "topic": "approval", "data": {"request_id": "r1"}},
            topic="approval",
        )

        assert delivered == 1
        assert not readonly.sent
        assert operator.sent


class TestTopicFiltering:
    async def test_only_subscribers_receive_the_topic(self, api_state) -> None:
        wants, does_not = _FakeWS(), _FakeWS()
        api_state._ws_clients.update({wants, does_not})
        await api_state.set_ws_topics(wants, frozenset({"price"}))
        await api_state.set_ws_topics(does_not, frozenset({"equity"}))

        delivered = await api_state.broadcast(
            {"type": "event", "topic": "price", "data": {"mid": 1.0}}, topic="price"
        )

        assert delivered == 1
        assert wants.sent and not does_not.sent

    async def test_a_hidden_panel_costs_zero_bytes(self, api_state) -> None:
        """The point of subscriptions: not subscribing means not receiving."""
        client = _FakeWS()
        api_state._ws_clients.add(client)
        await api_state.set_ws_topics(client, frozenset())

        assert await api_state.broadcast({"type": "event"}, topic="book") == 0
        assert not client.sent

    async def test_filtering_still_serializes_once(self, api_state) -> None:
        """
        Every recipient gets the *same string*. Identity, not equality: a
        per-recipient payload is the O(clients) cost this design deletes.
        """
        a, b, c = _FakeWS(), _FakeWS(), _FakeWS()
        api_state._ws_clients.update({a, b, c})
        for ws in (a, b, c):
            await api_state.set_ws_topics(ws, frozenset({"regime"}))

        await api_state.broadcast(
            {"type": "event", "topic": "regime", "data": {"state": 1}}, topic="regime"
        )

        assert a.sent[0] == b.sent[0] == c.sent[0]

    async def test_an_untopiced_broadcast_still_reaches_everyone(self, api_state) -> None:
        """
        The heartbeat and control_changed have no topic and must not be
        filtered -- the heartbeat is the resync anchor and the liveness
        signal, and a client that subscribed to nothing still needs it.
        """
        client = _FakeWS()
        api_state._ws_clients.add(client)
        await api_state.set_ws_topics(client, frozenset())

        assert await api_state.broadcast({"type": "tick"}) == 1


class TestSubscribeCommand:
    async def test_a_valid_subscribe_narrows_the_set(self, api_state) -> None:
        from src.api import main as api_main

        ws = _FakeWS()
        api_state._ws_clients.add(ws)
        api_state._ws_roles[ws] = Role.TRADE_AUTHORIZING

        await api_main._handle_ws_command(ws, json.dumps({"op": "subscribe", "topics": ["price"]}))

        assert api_state._ws_topics[ws] == frozenset({"price"})

    async def test_an_unknown_topic_is_refused_explicitly(self, api_state) -> None:
        """
        Silently accepting it yields a panel that renders nothing with no
        error anywhere -- the hardest failure to diagnose from outside.
        """
        from src.api import main as api_main

        ws = _FakeWS()
        api_state._ws_clients.add(ws)
        api_state._ws_roles[ws] = Role.TRADE_AUTHORIZING

        await api_main._handle_ws_command(
            ws, json.dumps({"op": "subscribe", "topics": ["price", "nonsense"]})
        )

        assert ws.sent, "the client must be told"
        assert "nonsense" in ws.sent[0]

    async def test_a_readonly_subscribe_to_approval_is_refused_and_not_granted(
        self, api_state
    ) -> None:
        from src.api import main as api_main

        ws = _FakeWS()
        api_state._ws_clients.add(ws)
        api_state._ws_roles[ws] = Role.READ_ONLY

        await api_main._handle_ws_command(
            ws, json.dumps({"op": "subscribe", "topics": ["approval", "equity"]})
        )

        # Told about the refusal, and -- the part that matters -- not granted.
        assert "approval" in ws.sent[0]
        assert api_state._ws_topics[ws] == frozenset({"equity"})

    async def test_junk_is_ignored_without_closing_the_socket(self, api_state) -> None:
        """
        A malformed subscribe is a client bug, not a prober. Closing would
        take the working panels down with the broken one -- deliberately
        unlike the frame guard's close-on-violation.
        """
        from src.api import main as api_main

        ws = _FakeWS()
        api_state._ws_clients.add(ws)
        api_state._ws_roles[ws] = Role.TRADE_AUTHORIZING
        await api_state.set_ws_topics(ws, frozenset({"price"}))

        await api_main._handle_ws_command(ws, "not json at all")
        await api_main._handle_ws_command(ws, json.dumps({"op": "something_else"}))

        assert api_state._ws_topics[ws] == frozenset({"price"})

    async def test_topics_must_be_a_list_of_strings(self, api_state) -> None:
        from src.api import main as api_main

        ws = _FakeWS()
        api_state._ws_clients.add(ws)
        api_state._ws_roles[ws] = Role.TRADE_AUTHORIZING

        await api_main._handle_ws_command(ws, json.dumps({"op": "subscribe", "topics": "price"}))

        assert ws.sent and "list of strings" in ws.sent[0]


class _ScriptedWS(_FakeWS):
    """
    A socket that hands the reader a scripted list of frames, then hangs up.

    Enough of the real surface for the endpoint and the reader: accept and
    close are recorded rather than performed, and `client` is read only by
    the connect/disconnect log lines.
    """

    client = "testclient"

    def __init__(self, frames: list[str] | None = None) -> None:
        super().__init__()
        self._frames = list(frames or [])
        self.accepted = False
        self.close_codes: list[int] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)

    async def receive_text(self) -> str:
        from fastapi import WebSocketDisconnect

        if not self._frames:
            raise WebSocketDisconnect(code=1000)
        return self._frames.pop(0)


def _client_frame(topics: list[str]) -> str:
    """
    The frame the dashboard actually sends -- see `_sendSubscribe` in
    frontend/src/hooks/useApi.js.

    Built here the way the client builds it, deliberately: the guard's
    envelope and the command's payload are enforced by two different pieces
    of code, and a client that satisfies one and not the other is closed on
    every connect.
    """
    import time

    return json.dumps(
        {
            "type": "subscribe",
            "nonce": f"s-{int(time.time() * 1000):x}-sub",
            "ts": time.time(),
            "op": "subscribe",
            "topics": topics,
        }
    )


class TestTheFrameTheClientSends:
    async def test_it_survives_the_guard_and_narrows_the_subscription(self, api_state) -> None:
        """
        End to end through `_guarded_ws_reader`, not straight into the
        handler. The guard refuses a frame without `type`, `nonce` and `ts`
        and *closes the socket* for it, so a subscribe carrying only `op` and
        `topics` would turn every connect into a disconnect -- and the
        reconnect would send the same frame again. Driving the real reader is
        what makes that reachable by a test at all.
        """
        from src.api import main as api_main

        ws = _ScriptedWS([_client_frame(["price", "equity"])])
        api_state._ws_clients.add(ws)
        api_state._ws_roles[ws] = Role.TRADE_AUTHORIZING

        await api_main._guarded_ws_reader(ws)

        assert api_state._ws_topics[ws] == frozenset({"price", "equity"})
        assert ws.close_codes == []
        assert ws.sent == []

    async def test_a_frame_without_the_envelope_is_closed_not_subscribed(self, api_state) -> None:
        """
        The other half of the same fact, pinned so the client cannot quietly
        go back to sending a bare command: the guard closes it, and nothing
        is subscribed.
        """
        from src.api import main as api_main

        ws = _ScriptedWS([json.dumps({"op": "subscribe", "topics": ["price"]})])
        api_state._ws_clients.add(ws)
        api_state._ws_roles[ws] = Role.TRADE_AUTHORIZING

        await api_main._guarded_ws_reader(ws)

        assert ws.close_codes  # closed on policy violation
        assert ws not in api_state._ws_topics


class TestTheConnectPath:
    async def test_a_new_connection_starts_subscribed_to_what_its_key_allows(
        self, api_state, monkeypatch
    ) -> None:
        """
        Defaulting to nothing would silence every dashboard shipped before
        subscriptions existed, which is worse than sending too much. A
        read-only key still never starts subscribed to `approval`.
        """
        from unittest.mock import AsyncMock

        from src.api import main as api_main

        ws = _ScriptedWS()
        monkeypatch.setattr(api_main, "verify_ws_key", AsyncMock(return_value=Role.READ_ONLY))
        monkeypatch.setattr(api_main, "_build_tick_snapshot", AsyncMock(return_value=None))
        granted: dict[object, frozenset[str]] = {}
        real_set = api_state.set_ws_topics

        async def _record(sock, topics):
            granted[sock] = topics
            await real_set(sock, topics)

        monkeypatch.setattr(api_state, "set_ws_topics", _record)

        await api_main.websocket_endpoint(ws)

        assert ws.accepted
        assert granted[ws] == api_state.permitted_topics(Role.READ_ONLY)
        assert "approval" not in granted[ws]
        # The finally arm ran: a hung-up connection leaves nothing behind.
        assert ws not in api_state._ws_clients
        assert ws not in api_state._ws_roles

    async def test_the_cold_start_snapshot_goes_out_before_the_first_beat(
        self, api_state, monkeypatch
    ) -> None:
        """
        Without it a client that connects just after a beat renders empty
        panels for most of a heartbeat, which reads as a broken dashboard.
        """
        from unittest.mock import AsyncMock

        from src.api import main as api_main

        ws = _ScriptedWS()
        monkeypatch.setattr(api_main, "verify_ws_key", AsyncMock(return_value=Role.READ_ONLY))
        monkeypatch.setattr(
            api_main, "_build_tick_snapshot", AsyncMock(return_value={"type": "tick"})
        )

        await api_main.websocket_endpoint(ws)

        assert json.loads(ws.sent[0]) == {"type": "tick"}
