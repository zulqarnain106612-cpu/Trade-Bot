"""
Exchange flow provider for E-18 (network / flow-graph engine).

Source:
  - DefiLlama /cexs (no auth, free) — per-CEX reserves and net inflows.

This yields *aggregate netflow per exchange*, not labelled pairwise transfers.
Pairwise exchange-to-exchange edges require address labelling, which is only
sold by paid providers (Arkham, Nansen). We therefore emit a bipartite flow
graph: a synthetic ``MARKET`` node on one side, every CEX on the other, with
edge direction set by the sign of the netflow. E-18 detects this topology and
switches to a metric that is valid on it — see ``src/engines/e18_network.py``.

Records are persisted per-day so that out-of-sample history accumulates: the
/cexs endpoint is a snapshot with no historical counterpart, so a forward
record is the only way E-18 can ever be validated.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pandas as pd
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_CEXS_URL = "https://api.llama.fi/cexs"
_POLL_INTERVAL = 900  # 15 minutes — upstream refreshes far slower than a tick

#: Synthetic counterparty node representing everything that is not a CEX.
MARKET_NODE = "MARKET"

#: Tags every record this provider emits, so E-18 can tell netflow-derived
#: edges apart from genuine pairwise ones without guessing from topology.
SOURCE_TAG = "defillama_cex_netflow"

#: Netflows below this are upstream rounding noise, not signal.
_MIN_ABS_USD = 1_000.0


def build_flow_records(payload: Any, window: str = "inflows_24h") -> list[dict[str, Any]]:
    """Convert a DefiLlama /cexs payload into E-18 flow-edge records.

    A positive netflow means capital moved *into* the exchange, so the edge
    runs MARKET -> exchange. A negative netflow is a withdrawal: exchange ->
    MARKET. ``usd_volume`` is always the magnitude; direction carries the sign.
    """
    rows = payload.get("cexs", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []

    records: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        net = row.get(window)
        # currentTvl is absent for exchanges DefiLlama cannot attribute wallets
        # to; their netflow is not meaningful.
        if not name or net is None or row.get("currentTvl") is None:
            continue
        try:
            net_f = float(net)
        except (TypeError, ValueError):
            continue
        if abs(net_f) < _MIN_ABS_USD:
            continue

        src, dst = (MARKET_NODE, name) if net_f > 0 else (name, MARKET_NODE)
        records.append(
            {
                "from": src,
                "to": dst,
                "usd_volume": abs(net_f),
                "net_usd": net_f,
                "exchange": name,
                "source": SOURCE_TAG,
            }
        )
    return records


class ExchangeFlowProvider:
    def __init__(self, data_root: Path = Path("data"), window: str = "inflows_24h") -> None:
        self._data_root = data_root
        self._window = window
        self._flows: list[dict[str, Any]] = []

    def latest_flows(self) -> list[dict[str, Any]]:
        return list(self._flows)

    async def run_loop(self) -> None:
        while True:
            await self._fetch()
            await asyncio.sleep(_POLL_INTERVAL)

    async def fetch_once(self) -> list[dict[str, Any]]:
        await self._fetch()
        return self.latest_flows()

    async def _fetch(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(_CEXS_URL, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    payload = await resp.json()
            flows = build_flow_records(payload, self._window)
            if not flows:
                log.warning("exchange_flow_empty", window=self._window)
                return
            self._flows = flows
            # Cache first: the tick path reads it, and its freshness must not
            # depend on the daily history write succeeding.
            self._update_cache()
            self._persist()
        except Exception as exc:
            log.warning("exchange_flow_fetch_error", exc=str(exc))

    def _update_cache(self) -> None:
        try:
            from src.data.provider_cache import get_provider_cache

            get_provider_cache().set_exchange_flows(self._flows)
        except Exception:
            pass

    def _persist(self) -> None:
        """Append today's snapshot so forward history builds for validation."""
        try:
            now = datetime.now(UTC)
            path = self._data_root / "exchange_flows" / f"{now.strftime('%Y-%m-%d')}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            frame = pd.DataFrame(self._flows)
            frame["timestamp_utc"] = now
            if path.exists():
                frame = pd.concat([pd.read_parquet(path), frame], ignore_index=True)
            frame.to_parquet(path, index=False)
        except Exception as exc:
            log.warning("exchange_flow_persist_error", exc=str(exc))
