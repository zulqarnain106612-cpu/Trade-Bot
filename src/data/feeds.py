"""
Redpanda (Kafka-compatible) feed producer.

Wraps ccxt WebSocket order book / trade feed and publishes normalized
tick events onto the Redpanda bus so all downstream consumers (feature
workers, timescale writer, DuckDB OLAP) can subscribe independently.

Topics (all prefixed with `crypto.`):
  crypto.ohlcv.<symbol>.<timeframe>  — OHLCV bar close events
  crypto.orderbook.<symbol>          — L2 order book snapshot
  crypto.trades.<symbol>             — individual trade ticks
  crypto.ecc                         — ECC pipeline output

REDPANDA_BROKERS env var controls broker address (default localhost:9092).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_BROKERS = os.environ.get("REDPANDA_BROKERS", "localhost:9092")


def _make_producer() -> Any:
    try:
        from aiokafka import AIOKafkaProducer  # type: ignore[import]

        return AIOKafkaProducer(
            bootstrap_servers=_BROKERS,
            value_serializer=lambda v: json.dumps(v).encode(),
            compression_type="lz4",
            acks="all",
            request_timeout_ms=5000,
            retry_backoff_ms=200,
        )
    except ImportError:
        log.warning("aiokafka_not_installed_redpanda_disabled")
        return None


class RedpandaFeeds:
    """
    Async producer that publishes exchange events onto the Redpanda bus.

    Usage:
        feeds = RedpandaFeeds()
        await feeds.start()
        await feeds.publish_ohlcv("BTC/USDT", "1h", bar_dict)
        await feeds.stop()
    """

    def __init__(self) -> None:
        self._producer: Any | None = None
        self._enabled = bool(_BROKERS)

    async def start(self) -> None:
        if not self._enabled:
            return
        self._producer = _make_producer()
        if self._producer is not None:
            try:
                await self._producer.start()
                log.info("redpanda_producer_started", brokers=_BROKERS)
            except Exception as exc:
                log.warning("redpanda_producer_start_failed", exc=str(exc))
                self._producer = None

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def publish_ohlcv(self, symbol: str, timeframe: str, bar: dict) -> None:
        topic = f"crypto.ohlcv.{symbol.replace('/', '_')}.{timeframe}"
        payload = {
            "ts": int(time.time() * 1000),
            "symbol": symbol,
            "timeframe": timeframe,
            **bar,
        }
        await self._send(topic, payload)

    async def publish_orderbook(self, symbol: str, orderbook: dict) -> None:
        topic = f"crypto.orderbook.{symbol.replace('/', '_')}"
        payload = {
            "ts": int(time.time() * 1000),
            "symbol": symbol,
            "bids": orderbook.get("bids", [])[:20],
            "asks": orderbook.get("asks", [])[:20],
        }
        await self._send(topic, payload)

    async def publish_trade(self, symbol: str, trade: dict) -> None:
        topic = f"crypto.trades.{symbol.replace('/', '_')}"
        await self._send(topic, {"ts": int(time.time() * 1000), "symbol": symbol, **trade})

    async def publish_ecc(self, ecc_result: dict) -> None:
        await self._send("crypto.ecc", {"ts": int(time.time() * 1000), **ecc_result})

    async def _send(self, topic: str, payload: dict) -> None:
        if self._producer is None:
            return
        try:
            await self._producer.send_and_wait(topic, payload)
        except Exception as exc:
            log.warning("redpanda_send_failed", topic=topic, exc=str(exc))


class RedpandaConsumer:
    """
    Async consumer that reads from a Redpanda topic and yields messages.

    Usage:
        async for msg in RedpandaConsumer("crypto.ecc", group_id="risk-worker"):
            process(msg)
    """

    def __init__(self, topic: str, group_id: str = "crypto-intel") -> None:
        self._topic = topic
        self._group_id = group_id
        self._consumer: Any | None = None

    async def __aenter__(self) -> RedpandaConsumer:
        try:
            from aiokafka import AIOKafkaConsumer  # type: ignore[import]

            self._consumer = AIOKafkaConsumer(
                self._topic,
                bootstrap_servers=_BROKERS,
                group_id=self._group_id,
                value_deserializer=lambda v: json.loads(v.decode()),
                auto_offset_reset="latest",
                enable_auto_commit=True,
            )
            await self._consumer.start()
        except ImportError:
            log.warning("aiokafka_not_installed_consumer_disabled")
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._consumer is not None:
            await self._consumer.stop()

    def __aiter__(self) -> RedpandaConsumer:
        return self

    async def __anext__(self) -> dict:
        if self._consumer is None:
            await asyncio.sleep(1)
            raise StopAsyncIteration
        msg = await self._consumer.getone()
        return msg.value  # type: ignore[no-any-return]
