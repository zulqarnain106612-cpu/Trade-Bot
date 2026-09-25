"""
Binance WebSocket orderbook + aggregate trade stream.

Writes parquet snapshots for E-02 (microstructure) and E-16/E-17
(adversarial / liquidity stress) engines.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from src.eventbus import get_event_bus

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_BINANCE_WS = "wss://stream.binance.com:9443/ws"
_MAX_SPREAD_BPS = 200

# How often the depth stream is allowed to fan out, in seconds.
#
# The raw stream arrives every 100ms, which is far faster than any display
# needs and far faster than it is safe to broadcast: unthrottled, each message
# would build a DataFrame for the provider cache and push a frame to every
# connected dashboard, ten times a second. Coalescing to 250ms keeps the
# display current to within a quarter second while cutting that work by 60%.
_PUBLISH_INTERVAL_S = 0.25


@dataclass
class OrderbookSnapshot:
    timestamp_utc: datetime
    bids_json: str
    asks_json: str
    mid: float
    spread_bps: float


@dataclass
class TradeEvent:
    timestamp_utc: datetime
    price: float
    qty: float
    is_buyer_maker: bool


@dataclass
class OrderbookStream:
    symbol: str  # e.g. "btcusdt"
    data_root: Path = field(default_factory=lambda: Path("data"))
    publish_interval_s: float = _PUBLISH_INTERVAL_S
    _snapshots: list[OrderbookSnapshot] = field(default_factory=list, repr=False)
    _trades: list[TradeEvent] = field(default_factory=list, repr=False)
    _running: bool = field(default=False, repr=False)
    # Monotonic, not wall clock: this gates a rate, and a clock step (NTP
    # correction, DST on a badly configured host) must not be able to stall
    # the feed or open the floodgate.
    _last_publish: float = field(default=0.0, repr=False)
    # When the last depth message landed, on the monotonic clock. Drives the
    # staleness test in latest_mid; None until the first message arrives.
    _last_depth_at: float | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        await asyncio.gather(
            self._stream_depth(),
            self._stream_trades(),
            return_exceptions=True,
        )

    def stop(self) -> None:
        self._running = False

    def latest_snapshot(self) -> OrderbookSnapshot | None:
        return self._snapshots[-1] if self._snapshots else None

    def latest_mid(self, max_age_s: float = 10.0) -> float | None:
        """
        Freshest mid price, or None if the stream has gone quiet.

        The staleness bound is the whole point. A disconnected socket leaves
        the last snapshot sitting there looking like a price, and a position
        marked against a price from four minutes ago is worse than one not
        marked at all -- it is wrong with no indication that it is wrong.
        Callers treat None as "fall back to REST".

        Age comes from the monotonic clock, not from the snapshot's own
        `timestamp_utc`. That field is a point in history and correct for the
        parquet record, but subtracting two wall-clock readings to get a
        duration means an NTP correction can make a live feed look stale --
        or, worse, make a dead one look fresh.
        """
        snap = self.latest_snapshot()
        if snap is None or self._last_depth_at is None:
            return None
        if time.monotonic() - self._last_depth_at > max_age_s:
            return None
        return snap.mid

    def recent_trades(self, n: int = 500) -> list[TradeEvent]:
        return self._trades[-n:]

    # ------------------------------------------------------------------
    # Depth stream
    # ------------------------------------------------------------------

    async def _stream_depth(self) -> None:
        uri = f"{_BINANCE_WS}/{self.symbol}@depth20@100ms"
        while self._running:
            try:
                async with websockets.connect(uri) as ws:
                    async for raw in ws:
                        if not self._running:
                            break
                        self._handle_depth(json.loads(raw))
            except ConnectionClosed:
                await asyncio.sleep(1)
            except Exception as exc:
                log.warning("depth_stream_error", exc=str(exc))
                await asyncio.sleep(2)

    def _handle_depth(self, msg: dict[str, Any]) -> None:
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])
        if not bids or not asks:
            return
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2
        spread_bps = (best_ask - best_bid) / mid * 10_000
        if spread_bps > _MAX_SPREAD_BPS:
            log.warning("depth_quality_reject", spread_bps=spread_bps)
            return
        snap = OrderbookSnapshot(
            timestamp_utc=datetime.now(UTC),
            bids_json=json.dumps(bids),
            asks_json=json.dumps(asks),
            mid=mid,
            spread_bps=spread_bps,
        )
        self._snapshots.append(snap)
        now_mono = time.monotonic()
        self._last_depth_at = now_mono

        # Everything below is rate-limited together. Appending the snapshot is
        # cheap and must happen on every message -- the parquet record and the
        # engines that read it depend on the full stream. Rebuilding a
        # DataFrame and fanning out to every dashboard is neither, and doing
        # both ten times a second was affordable only while this module was
        # unreferenced. Wiring it into the runtime is what makes the cost real.
        if now_mono - self._last_publish < self.publish_interval_s:
            if len(self._snapshots) >= 1000:
                self._flush_orderbook()
            return
        self._last_publish = now_mono

        try:
            from src.data.provider_cache import get_provider_cache

            get_provider_cache().set_orderbook(self.symbol, self._snapshots_as_df())
        except Exception as exc:
            log.warning(
                "provider_cache_publish_failed",
                field="orderbook",
                symbol=self.symbol,
                exc=str(exc),
            )

        bus = get_event_bus()
        bus.publish("price", {"symbol": self.symbol, "mid": mid, "spread_bps": spread_bps})
        bus.publish(
            "book",
            {
                "symbol": self.symbol,
                "mid": mid,
                "spread_bps": spread_bps,
                # Five levels, not twenty. The dashboard renders a ladder, and
                # the other fifteen are bytes multiplied by every connected
                # client for depth nobody is looking at.
                "bids": bids[:5],
                "asks": asks[:5],
            },
        )

        if len(self._snapshots) >= 1000:
            self._flush_orderbook()

    def _snapshots_as_df(self) -> pd.DataFrame:
        rows = [
            {
                "timestamp_utc": s.timestamp_utc,
                "bids_json": s.bids_json,
                "asks_json": s.asks_json,
                "mid": s.mid,
                "spread_bps": s.spread_bps,
            }
            for s in self._snapshots[-100:]  # last 100 snapshots for the cache
        ]
        return pd.DataFrame(rows)

    def _flush_orderbook(self) -> None:
        if not self._snapshots:
            return
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self.data_root / "orderbook" / self.symbol / f"{date_str}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "timestamp_utc": s.timestamp_utc,
                "bids_json": s.bids_json,
                "asks_json": s.asks_json,
                "mid": s.mid,
                "spread_bps": s.spread_bps,
            }
            for s in self._snapshots
        ]
        df = pd.DataFrame(rows)
        if path.exists():
            existing = pd.read_parquet(path)
            df = pd.concat([existing, df], ignore_index=True)
        df.to_parquet(path, index=False)
        self._snapshots.clear()

    # ------------------------------------------------------------------
    # Trade stream
    # ------------------------------------------------------------------

    async def _stream_trades(self) -> None:
        uri = f"{_BINANCE_WS}/{self.symbol}@aggTrade"
        while self._running:
            try:
                async with websockets.connect(uri) as ws:
                    async for raw in ws:
                        if not self._running:
                            break
                        self._handle_trade(json.loads(raw))
            except ConnectionClosed:
                await asyncio.sleep(1)
            except Exception as exc:
                log.warning("trade_stream_error", exc=str(exc))
                await asyncio.sleep(2)

    def _handle_trade(self, msg: dict[str, Any]) -> None:
        evt = TradeEvent(
            timestamp_utc=datetime.fromtimestamp(msg["T"] / 1000, tz=UTC),
            price=float(msg["p"]),
            qty=float(msg["q"]),
            is_buyer_maker=bool(msg["m"]),
        )
        self._trades.append(evt)
        if len(self._trades) >= 5000:
            self._flush_trades()

    def _flush_trades(self) -> None:
        if not self._trades:
            return
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self.data_root / "trades" / self.symbol / f"{date_str}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [
            {
                "timestamp_utc": t.timestamp_utc,
                "price": t.price,
                "qty": t.qty,
                "is_buyer_maker": t.is_buyer_maker,
            }
            for t in self._trades
        ]
        df = pd.DataFrame(rows)
        if path.exists():
            existing = pd.read_parquet(path)
            df = pd.concat([existing, df], ignore_index=True)
        df.to_parquet(path, index=False)
        self._trades.clear()
