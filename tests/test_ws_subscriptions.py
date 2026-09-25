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
