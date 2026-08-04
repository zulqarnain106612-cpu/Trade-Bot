"""
Binance WebSocket orderbook + aggregate trade stream.

Writes parquet snapshots for E-02 (microstructure) and E-16/E-17
(adversarial / liquidity stress) engines.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import structlog
import websockets
from websockets.exceptions import ConnectionClosed


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_BINANCE_WS = "wss://stream.binance.com:9443/ws"
_MAX_SPREAD_BPS = 200


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
    _snapshots: list[OrderbookSnapshot] = field(default_factory=list, repr=False)
    _trades: list[TradeEvent] = field(default_factory=list, repr=False)
    _running: bool = field(default=False, repr=False)

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
        try:
            from src.data.provider_cache import get_provider_cache

            get_provider_cache().set_orderbook(self.symbol, self._snapshots_as_df())
        except Exception:
            pass
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
