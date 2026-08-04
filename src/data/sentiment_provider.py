"""
Sentiment data provider for E-14 (sentiment quantification engine).

Sources:
  - alternative.me Fear & Greed Index (no auth, free)
  - RSS NLP headlines via VADER (CoinDesk, CryptoSlate)
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET  # nosec B314 — RSS data from known public feeds only
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pandas as pd
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_FG_URL = "https://api.alternative.me/fapi/v2/fear-and-greed-index/?limit=1"
_RSS_SOURCES = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cryptoslate.com/feed/",
]
_FG_POLL_INTERVAL = 3600  # 1 hour
_RSS_POLL_INTERVAL = 900  # 15 minutes


class SentimentProvider:
    def __init__(self, data_root: Path = Path("data")) -> None:
        self._data_root = data_root
        self._fg_score: float = 50.0
        self._fg_label: str = "Neutral"
        self._headlines: list[dict] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def latest_fg(self) -> tuple[float, str]:
        return self._fg_score, self._fg_label

    def recent_headlines(self, n: int = 20) -> list[dict]:
        return self._headlines[-n:]

    async def run_fg_loop(self) -> None:
        while True:
            await self._fetch_fg()
            await asyncio.sleep(_FG_POLL_INTERVAL)

    async def run_rss_loop(self) -> None:
        while True:
            await self._fetch_rss()
            await asyncio.sleep(_RSS_POLL_INTERVAL)

    async def fetch_once(self) -> dict:
        await asyncio.gather(self._fetch_fg(), self._fetch_rss())
        return {"fg_score": self._fg_score, "fg_label": self._fg_label}

    # ------------------------------------------------------------------
    # Fear & Greed
    # ------------------------------------------------------------------

    async def _fetch_fg(self) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(_FG_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    entry = data["data"][0]
                    self._fg_score = float(entry["value"])
                    self._fg_label = entry["value_classification"]
                    self._persist_fg()
        except Exception as exc:
            log.warning("fg_fetch_error", exc=str(exc))

    def _persist_fg(self) -> None:
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self._data_root / "sentiment" / f"{date_str}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp_utc": [datetime.now(UTC)],
            "fg_score": [self._fg_score],
            "fg_label": [self._fg_label],
            "source": ["fear_greed"],
            "headline": [""],
            "vader_compound": [0.0],
        }
        self._append_parquet(path, pd.DataFrame(row))

    # ------------------------------------------------------------------
    # RSS NLP
    # ------------------------------------------------------------------

    async def _fetch_rss(self) -> None:
        try:
            vader = self._get_vader()
            rows = []
            async with aiohttp.ClientSession() as session:
                for url in _RSS_SOURCES:
                    try:
                        async with session.get(
                            url, timeout=aiohttp.ClientTimeout(total=10)
                        ) as resp:
                            text = await resp.text()
                            rows.extend(self._parse_rss(text, url, vader))
                    except Exception as exc:
                        log.warning("rss_source_error", url=url, exc=str(exc))
            if rows:
                self._headlines.extend(rows)
                if len(self._headlines) > 2000:
                    self._headlines = self._headlines[-2000:]
                self._persist_headlines(rows)
        except Exception as exc:
            log.warning("rss_fetch_error", exc=str(exc))

    def _parse_rss(self, xml_text: str, source: str, vader: object) -> list[dict]:
        rows = []
        try:
            root = ET.fromstring(xml_text)  # nosec B314 -- RSS from known public feeds
            for item in root.findall(".//item"):
                title_el = item.find("title")
                if title_el is None or not title_el.text:
                    continue
                headline = title_el.text.strip()
                score = vader.polarity_scores(headline)["compound"]  # type: ignore[attr-defined]
                rows.append(
                    {
                        "timestamp_utc": datetime.now(UTC),
                        "headline": headline,
                        "vader_compound": score,
                        "source": source,
                        "fg_score": self._fg_score,
                        "fg_label": self._fg_label,
                    }
                )
        except ET.ParseError:
            pass
        return rows

    def _persist_headlines(self, rows: list[dict]) -> None:
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self._data_root / "sentiment" / f"{date_str}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._append_parquet(path, pd.DataFrame(rows))

    @staticmethod
    def _get_vader() -> object:
        try:
            from vaderSentiment.vaderSentiment import (
                SentimentIntensityAnalyzer,  # type: ignore[import]
            )

            return SentimentIntensityAnalyzer()
        except ImportError:
            # Fallback: neutral scorer when vaderSentiment not installed
            class _Neutral:
                def polarity_scores(self, text: str) -> dict:
                    return {"compound": 0.0}

            return _Neutral()

    @staticmethod
    def _append_parquet(path: Path, df: pd.DataFrame) -> None:
        if path.exists():
            existing = pd.read_parquet(path)
            df = pd.concat([existing, df], ignore_index=True)
        df.to_parquet(path, index=False)
