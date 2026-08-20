"""
Block height provider for E-10 (stock-to-flow) and E-04 (halving overlay).

Source:
  - blockchain.info /q/getblockcount (no auth, free) — BTC tip height.

Both engines read ``data["block_height"]`` and nothing ever wrote it, so E-10
fell back to a hardcoded stock-to-flow ratio and E-04's halving overlay sat on
height 0. Emission maths only moves once every ten minutes, so a slow poll is
ample.
"""

from __future__ import annotations

import asyncio

import aiohttp
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_HEIGHT_URL = "https://blockchain.info/q/getblockcount"
_POLL_INTERVAL = 600  # 10 minutes — one BTC block

#: Sanity bounds. Guards against a parsed error page becoming a "height" that
#: silently shifts the emission epoch and every fair value with it.
_MIN_PLAUSIBLE_HEIGHT = 800_000
_MAX_PLAUSIBLE_HEIGHT = 10_000_000


class BlockHeightProvider:
    def __init__(self) -> None:
        self._height: int = 0

    def latest_height(self) -> int:
        return self._height

    async def run_loop(self) -> None:
        while True:
            await self._fetch()
            await asyncio.sleep(_POLL_INTERVAL)

    async def fetch_once(self) -> int:
        await self._fetch()
        return self._height

    async def _fetch(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    _HEIGHT_URL, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    raw = (await resp.text()).strip()
            height = int(raw)
            if not _MIN_PLAUSIBLE_HEIGHT <= height <= _MAX_PLAUSIBLE_HEIGHT:
                log.warning("block_height_implausible", height=height)
                return
            self._height = height
            self._update_cache()
        except Exception as exc:
            log.warning("block_height_fetch_error", exc=str(exc))

    def _update_cache(self) -> None:
        try:
            from src.data.provider_cache import get_provider_cache

            get_provider_cache().set_block_height(self._height)
        except Exception as exc:
            log.warning(
                "provider_cache_publish_failed", field="block_height", exc=str(exc)
            )
