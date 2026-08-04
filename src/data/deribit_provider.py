"""
Deribit REST provider — options chain for E-12 (options market signal engine).

BTC and ETH only (LTC/XMR have no Deribit options; E-12 skips and
redistributes weight to E-01 per Gap G-05 fix).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pandas as pd
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_BASE = "https://www.deribit.com/api/v2/public"
_SUPPORTED = {"BTC", "ETH"}
_POLL_INTERVAL = 60  # seconds


class DeribitProvider:
    def __init__(self, data_root: Path = Path("data")) -> None:
        self._data_root = data_root
        self._cache: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def supports(self, symbol: str) -> bool:
        coin = symbol.split("/")[0].upper()
        return coin in _SUPPORTED

    async def fetch(self, symbol: str) -> pd.DataFrame | None:
        """Return options chain for symbol; None if unsupported or data absent."""
        coin = symbol.split("/")[0].upper()
        if coin not in _SUPPORTED:
            return None
        df = await self._fetch_chain(coin)
        if df is not None:
            self._cache[coin] = df
            self._persist(coin, df)
            try:
                from src.data.provider_cache import get_provider_cache

                get_provider_cache().set_options(coin, df)
            except Exception:
                pass
        return self._cache.get(coin)

    async def run_loop(self, symbol: str) -> None:
        while True:
            await self.fetch(symbol)
            await asyncio.sleep(_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fetch_chain(self, coin: str) -> pd.DataFrame | None:
        try:
            async with aiohttp.ClientSession() as session:
                instruments = await self._get_instruments(session, coin)
                rows = []
                for inst in instruments:
                    name = inst["instrument_name"]
                    ob = await self._get_orderbook(session, name)
                    if ob is None:
                        continue
                    row = self._parse_row(inst, ob)
                    if row is not None:
                        rows.append(row)
                if not rows:
                    return None
                return pd.DataFrame(rows)
        except Exception as exc:
            log.warning("deribit_fetch_error", exc=str(exc))
            return None

    async def _get_instruments(self, session: aiohttp.ClientSession, coin: str) -> list[dict]:
        try:
            url = f"{_BASE}/get_instruments"
            params = {"currency": coin, "kind": "option", "expired": "false"}
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()
                return data.get("result", [])
        except Exception as exc:
            log.warning("deribit_get_instruments_error", coin=coin, exc=str(exc))
            return []

    async def _get_orderbook(self, session: aiohttp.ClientSession, instrument: str) -> dict | None:
        try:
            url = f"{_BASE}/get_order_book"
            params = {"instrument_name": instrument, "depth": 1}
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                return data.get("result")
        except Exception as exc:
            log.debug("deribit_get_orderbook_error", instrument=instrument, exc=str(exc))
            return None

    def _parse_row(self, inst: dict, ob: dict) -> dict | None:
        iv = ob.get("mark_iv", 0.0)
        oi = ob.get("open_interest", 0.0)
        if iv == 0.0 or oi == 0.0:
            return None  # quality gate
        greeks = ob.get("greeks", {})
        return {
            "timestamp_utc": datetime.now(UTC),
            "instrument": inst["instrument_name"],
            "expiry": inst.get("expiration_timestamp", 0),
            "strike": float(inst.get("strike", 0)),
            "option_type": inst.get("option_type", ""),
            "iv": float(iv),
            "oi": float(oi),
            "volume": float(ob.get("stats", {}).get("volume", 0)),
            "delta": float(greeks.get("delta", 0)),
            "gamma": float(greeks.get("gamma", 0)),
        }

    def _persist(self, coin: str, df: pd.DataFrame) -> None:
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self._data_root / "options" / coin / f"{date_str}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, index=False)
