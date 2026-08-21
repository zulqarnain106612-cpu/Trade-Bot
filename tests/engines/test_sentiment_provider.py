"""
Tests for the E-14 sentiment provider.

Covers the Fear & Greed fetch, the RSS/VADER path, parquet persistence and the
provider-cache publish — all of which were previously exercised only through
the live network path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.data.sentiment_provider import SentimentProvider


_FG_BODY = {"data": [{"value": "78", "value_classification": "Extreme Greed"}]}

_RSS_BODY = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Bitcoin rallies to a new high</title></item>
  <item><title>  Ether holds steady  </title></item>
  <item><title></title></item>
  <item><description>no title element</description></item>
</channel></rss>
"""


def _mock_session(*, json_body=None, text_body=None):
    resp = AsyncMock()
    resp.raise_for_status = MagicMock()
    if json_body is not None:
        resp.json = AsyncMock(return_value=json_body)
    if text_body is not None:
        resp.text = AsyncMock(return_value=text_body)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    session = AsyncMock()
    session.get = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


# ---------------------------------------------------------------------------
# Fear & Greed
# ---------------------------------------------------------------------------


class TestFearAndGreed:
    @pytest.mark.asyncio
    async def test_fetch_updates_score_and_persists(self, tmp_path: Path) -> None:
        p = SentimentProvider(data_root=tmp_path)
        with patch("aiohttp.ClientSession", return_value=_mock_session(json_body=_FG_BODY)):
            await p._fetch_fg()

        assert p.latest_fg() == (78.0, "Extreme Greed")
        files = list((tmp_path / "sentiment").glob("*.parquet"))
        assert len(files) == 1
        assert pd.read_parquet(files[0])["fg_score"].iloc[0] == 78.0

    @pytest.mark.asyncio
    async def test_fetch_keeps_the_neutral_default_on_error(self, tmp_path: Path) -> None:
        p = SentimentProvider(data_root=tmp_path)
        with patch("aiohttp.ClientSession", side_effect=RuntimeError("boom")):
            await p._fetch_fg()

        assert p.latest_fg() == (50.0, "Neutral")
        assert not (tmp_path / "sentiment").exists()

    @pytest.mark.asyncio
    async def test_second_fetch_appends_to_the_same_day_file(self, tmp_path: Path) -> None:
        p = SentimentProvider(data_root=tmp_path)
        with patch("aiohttp.ClientSession", return_value=_mock_session(json_body=_FG_BODY)):
            await p._fetch_fg()
            await p._fetch_fg()

        path = next((tmp_path / "sentiment").glob("*.parquet"))
        assert len(pd.read_parquet(path)) == 2


# ---------------------------------------------------------------------------
# RSS / VADER
# ---------------------------------------------------------------------------


class TestRss:
    @pytest.mark.asyncio
    async def test_fetch_collects_headlines_from_every_source(self, tmp_path: Path) -> None:
        p = SentimentProvider(data_root=tmp_path)
        with patch("aiohttp.ClientSession", return_value=_mock_session(text_body=_RSS_BODY)):
            await p._fetch_rss()

        # 2 usable titles per source, 2 sources; empty/missing titles are skipped.
        assert len(p.recent_headlines(100)) == 4
        headlines = [h["headline"] for h in p.recent_headlines(100)]
        assert "Bitcoin rallies to a new high" in headlines
        assert "Ether holds steady" in headlines  # whitespace stripped
        assert all(-1.0 <= h["vader_compound"] <= 1.0 for h in p.recent_headlines(100))
        assert list((tmp_path / "sentiment").glob("*.parquet"))

    def test_recent_headlines_returns_only_the_tail(self, tmp_path: Path) -> None:
        p = SentimentProvider(data_root=tmp_path)
        p._headlines = [{"headline": str(i), "vader_compound": 0.0} for i in range(50)]
        tail = p.recent_headlines(5)
        assert [h["headline"] for h in tail] == ["45", "46", "47", "48", "49"]

    @pytest.mark.asyncio
    async def test_a_failing_source_does_not_abort_the_fetch(self, tmp_path: Path) -> None:
        p = SentimentProvider(data_root=tmp_path)
        session = _mock_session(text_body=_RSS_BODY)
        session.get = MagicMock(side_effect=RuntimeError("source down"))
        with patch("aiohttp.ClientSession", return_value=session):
            await p._fetch_rss()

        assert p.recent_headlines() == []

    @pytest.mark.asyncio
    async def test_outer_failure_is_swallowed(self, tmp_path: Path) -> None:
        p = SentimentProvider(data_root=tmp_path)
        with patch("aiohttp.ClientSession", side_effect=RuntimeError("boom")):
            await p._fetch_rss()
        assert p.recent_headlines() == []

    @pytest.mark.asyncio
    async def test_headline_buffer_is_capped_at_2000(self, tmp_path: Path) -> None:
        p = SentimentProvider(data_root=tmp_path)
        p._headlines = [{"headline": "old", "vader_compound": 0.0} for _ in range(2000)]
        with patch("aiohttp.ClientSession", return_value=_mock_session(text_body=_RSS_BODY)):
            await p._fetch_rss()

        assert len(p._headlines) == 2000
        assert p._headlines[-1]["headline"] != "old"

    def test_malformed_xml_yields_no_rows(self) -> None:
        p = SentimentProvider(data_root=Path("data"))
        assert p._parse_rss("<not-xml", "src", p._get_vader()) == []

    def test_vader_fallback_scores_neutral_when_the_package_is_missing(self) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("vaderSentiment"):
                raise ImportError("no vaderSentiment")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", fake_import):
            vader = SentimentProvider._get_vader()

        assert vader.polarity_scores("anything at all")["compound"] == 0.0


# ---------------------------------------------------------------------------
# Cache publish + loops
# ---------------------------------------------------------------------------


class TestCacheAndLoops:
    def test_cache_receives_the_average_vader_score(self, tmp_path: Path) -> None:
        p = SentimentProvider(data_root=tmp_path)
        p._headlines = [
            {"headline": "a", "vader_compound": 0.5},
            {"headline": "b", "vader_compound": -0.1},
        ]
        with patch("src.data.provider_cache.get_provider_cache") as get_cache:
            p._update_cache()

        _, _, vader_avg = get_cache.return_value.set_sentiment.call_args[0]
        assert vader_avg == pytest.approx(0.2)

    def test_cache_publish_failure_is_swallowed(self, tmp_path: Path) -> None:
        p = SentimentProvider(data_root=tmp_path)
        with patch(
            "src.data.provider_cache.get_provider_cache", side_effect=RuntimeError("no cache")
        ):
            p._update_cache()  # must not raise

    @pytest.mark.asyncio
    async def test_fetch_once_reports_the_current_fear_and_greed(self, tmp_path: Path) -> None:
        p = SentimentProvider(data_root=tmp_path)
        with (
            patch.object(p, "_fetch_fg", AsyncMock()),
            patch.object(p, "_fetch_rss", AsyncMock()),
        ):
            assert await p.fetch_once() == {"fg_score": 50.0, "fg_label": "Neutral"}

    @pytest.mark.asyncio
    async def test_fg_loop_fetches_then_sleeps(self, tmp_path: Path) -> None:
        p = SentimentProvider(data_root=tmp_path)
        calls: list[float] = []

        async def stop_after_first(delay: float) -> None:
            calls.append(delay)
            raise asyncio.CancelledError

        with (
            patch.object(p, "_fetch_fg", AsyncMock()) as fetch,
            patch("asyncio.sleep", stop_after_first),
            pytest.raises(asyncio.CancelledError),
        ):
            await p.run_fg_loop()

        fetch.assert_awaited_once()
        assert calls == [3600]

    @pytest.mark.asyncio
    async def test_rss_loop_fetches_then_sleeps(self, tmp_path: Path) -> None:
        p = SentimentProvider(data_root=tmp_path)
        calls: list[float] = []

        async def stop_after_first(delay: float) -> None:
            calls.append(delay)
            raise asyncio.CancelledError

        with (
            patch.object(p, "_fetch_rss", AsyncMock()) as fetch,
            patch("asyncio.sleep", stop_after_first),
            pytest.raises(asyncio.CancelledError),
        ):
            await p.run_rss_loop()

        fetch.assert_awaited_once()
        assert calls == [900]
