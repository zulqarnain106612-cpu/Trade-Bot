"""Tests for src/data/feeds.py -- the Redpanda/aiokafka feed producer.

aiokafka is an optional dependency not installed in CI, so `_make_producer`
naturally exercises the ImportError branch there. Tests inject a fake
`aiokafka` module via sys.modules to also cover the "installed" branch
without requiring a real broker.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.data.feeds as feeds
from src.data.feeds import RedpandaConsumer, RedpandaFeeds, _make_producer


def test_make_producer_returns_none_when_aiokafka_missing():
    with patch.dict(sys.modules, {"aiokafka": None}):
        assert _make_producer() is None


def test_make_producer_returns_instance_when_available():
    fake_module = MagicMock()
    fake_instance = MagicMock()
    fake_module.AIOKafkaProducer.return_value = fake_instance
    with patch.dict(sys.modules, {"aiokafka": fake_module}):
        producer = _make_producer()
    assert producer is fake_instance


async def test_start_noop_when_disabled():
    rf = RedpandaFeeds()
    rf._enabled = False
    await rf.start()
    assert rf._producer is None


async def test_start_sets_producer_when_make_producer_succeeds():
    rf = RedpandaFeeds()
    fake_producer = AsyncMock()
    with patch.object(feeds, "_make_producer", return_value=fake_producer):
        await rf.start()
    assert rf._producer is fake_producer
    fake_producer.start.assert_awaited_once()


async def test_start_clears_producer_when_start_raises():
    rf = RedpandaFeeds()
    fake_producer = AsyncMock()
    fake_producer.start.side_effect = RuntimeError("no broker")
    with patch.object(feeds, "_make_producer", return_value=fake_producer):
        await rf.start()
    assert rf._producer is None


async def test_start_when_make_producer_returns_none():
    rf = RedpandaFeeds()
    with patch.object(feeds, "_make_producer", return_value=None):
        await rf.start()
    assert rf._producer is None


async def test_stop_noop_when_no_producer():
    rf = RedpandaFeeds()
    await rf.stop()  # must not raise


async def test_stop_stops_and_clears_producer():
    rf = RedpandaFeeds()
    rf._producer = AsyncMock()
    fake_producer = rf._producer
    await rf.stop()
    fake_producer.stop.assert_awaited_once()
    assert rf._producer is None


async def test_send_noop_when_no_producer():
    rf = RedpandaFeeds()
    await rf._send("topic", {"a": 1})  # must not raise, no-op


async def test_send_delivers_payload():
    rf = RedpandaFeeds()
    rf._producer = AsyncMock()
    await rf._send("topic", {"a": 1})
    rf._producer.send_and_wait.assert_awaited_once_with("topic", {"a": 1})


async def test_send_swallows_exception():
    rf = RedpandaFeeds()
    rf._producer = AsyncMock()
    rf._producer.send_and_wait.side_effect = RuntimeError("broker down")
    await rf._send("topic", {"a": 1})  # must not raise


async def test_publish_ohlcv_builds_topic_and_payload():
    rf = RedpandaFeeds()
    with patch.object(rf, "_send", new=AsyncMock()) as mock_send:
        await rf.publish_ohlcv("BTC/USDT", "1h", {"close": 100})
    topic, payload = mock_send.call_args[0]
    assert topic == "crypto.ohlcv.BTC_USDT.1h"
    assert payload["symbol"] == "BTC/USDT"
    assert payload["close"] == 100


async def test_publish_orderbook_truncates_to_20_levels():
    rf = RedpandaFeeds()
    book = {"bids": [[i, i] for i in range(30)], "asks": [[i, i] for i in range(30)]}
    with patch.object(rf, "_send", new=AsyncMock()) as mock_send:
        await rf.publish_orderbook("ETH/USDT", book)
    topic, payload = mock_send.call_args[0]
    assert topic == "crypto.orderbook.ETH_USDT"
    assert len(payload["bids"]) == 20
    assert len(payload["asks"]) == 20


async def test_publish_trade_builds_topic():
    rf = RedpandaFeeds()
    with patch.object(rf, "_send", new=AsyncMock()) as mock_send:
        await rf.publish_trade("BTC/USDT", {"price": 50000})
    topic, payload = mock_send.call_args[0]
    assert topic == "crypto.trades.BTC_USDT"
    assert payload["price"] == 50000


async def test_publish_ecc_uses_fixed_topic():
    rf = RedpandaFeeds()
    with patch.object(rf, "_send", new=AsyncMock()) as mock_send:
        await rf.publish_ecc({"cluster_id": "x"})
    topic, payload = mock_send.call_args[0]
    assert topic == "crypto.ecc"
    assert payload["cluster_id"] == "x"


async def test_consumer_aenter_falls_back_when_aiokafka_missing():
    with patch.dict(sys.modules, {"aiokafka": None}):
        consumer = await RedpandaConsumer("crypto.ecc").__aenter__()
    assert consumer._consumer is None


async def test_consumer_aenter_starts_real_consumer_when_available():
    fake_module = MagicMock()
    fake_consumer_instance = AsyncMock()
    fake_module.AIOKafkaConsumer.return_value = fake_consumer_instance
    with patch.dict(sys.modules, {"aiokafka": fake_module}):
        consumer = await RedpandaConsumer("crypto.ecc", group_id="g1").__aenter__()
    assert consumer._consumer is fake_consumer_instance
    fake_consumer_instance.start.assert_awaited_once()


async def test_consumer_aexit_noop_without_consumer():
    consumer = RedpandaConsumer("topic")
    await consumer.__aexit__(None, None, None)  # must not raise


async def test_consumer_aexit_stops_consumer():
    consumer = RedpandaConsumer("topic")
    consumer._consumer = AsyncMock()
    fake = consumer._consumer
    await consumer.__aexit__(None, None, None)
    fake.stop.assert_awaited_once()


def test_consumer_aiter_returns_self():
    consumer = RedpandaConsumer("topic")
    assert consumer.__aiter__() is consumer


async def test_consumer_anext_without_consumer_raises_stop_async_iteration():
    consumer = RedpandaConsumer("topic")
    with patch("src.data.feeds.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        with pytest.raises(StopAsyncIteration):
            await consumer.__anext__()
    mock_sleep.assert_awaited_once_with(1)


async def test_consumer_anext_returns_message_value():
    consumer = RedpandaConsumer("topic")
    msg = MagicMock()
    msg.value = {"payload": 1}
    consumer._consumer = AsyncMock()
    consumer._consumer.getone.return_value = msg
    result = await consumer.__anext__()
    assert result == {"payload": 1}
