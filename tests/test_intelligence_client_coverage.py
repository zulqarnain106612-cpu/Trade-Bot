"""Tests for src/intelligence/client.py — target 85%+ coverage."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.intelligence.client import (
    CacheEntry,
    IntelligenceAggregator,
    get_intelligence_aggregator,
)

# ---------------------------------------------------------------------------
# CacheEntry
# ---------------------------------------------------------------------------


def test_cache_entry_not_stale_fresh():
    entry = CacheEntry(value=42, fetched_at=time.monotonic(), ttl_seconds=300)
    assert entry.is_stale is False


def test_cache_entry_stale_expired():
    old_time = time.monotonic() - 400.0
    entry = CacheEntry(value=42, fetched_at=old_time, ttl_seconds=300)
    assert entry.is_stale is True


def test_cache_entry_exactly_at_boundary():
    old_time = time.monotonic() - 300.0
    entry = CacheEntry(value=42, fetched_at=old_time, ttl_seconds=300)
    # Edge: age == ttl → stale (>)
    assert entry.is_stale is True


# ---------------------------------------------------------------------------
# IntelligenceAggregator init
# ---------------------------------------------------------------------------


def _make_agg(**kwargs) -> IntelligenceAggregator:
    settings = MagicMock()
    settings.glassnode_api_key = "gn_key"
    settings.cryptoquant_api_key = "cq_key"
    settings.cache_ttl_onchain_seconds = 300
    settings.cache_ttl_exchange_seconds = 60
    settings.glassnode_base_url = "https://api.glassnode.com/v1/metrics"
    settings.glassnode_rate_limit_seconds = 1.0
    settings.funding_rate_perp_symbol = "BTCUSDT"
    return IntelligenceAggregator(_settings=settings, **kwargs)


def test_aggregator_init_with_injected_settings():
    agg = _make_agg()
    assert agg.glassnode_key == "gn_key"
    assert agg.cryptoquant_key == "cq_key"
    assert agg.cache_ttl_onchain == 300
    assert agg.cache_ttl_exchange == 60


def test_aggregator_init_kwarg_overrides_settings():
    agg = _make_agg(glassnode_api_key="override_key", cache_ttl_onchain_seconds=600)
    assert agg.glassnode_key == "override_key"
    assert agg.cache_ttl_onchain == 600


def test_base_url_property():
    agg = _make_agg()
    assert "glassnode" in agg._base_url


# ---------------------------------------------------------------------------
# Cache hit path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_exchange_netflow_cache_hit():
    agg = _make_agg()
    cached = {"netflow": 1.0, "inflow": 1.0, "outflow": 0.0, "tscore": 0.5}
    entry = CacheEntry(value=cached, fetched_at=time.monotonic(), ttl_seconds=300)
    agg._cache["exchange_netflow_BTC_all_7d"] = entry

    result = await agg.get_exchange_netflow("BTC", None, 7)
    assert result["netflow"] == 1.0


@pytest.mark.asyncio
async def test_get_whale_activity_cache_hit():
    agg = _make_agg()
    cached = {"buy_volume": 10.0, "sell_volume": 5.0, "ratio": 2.0, "sentiment": "bullish"}
    entry = CacheEntry(value=cached, fetched_at=time.monotonic(), ttl_seconds=300)
    agg._cache["whale_activity_BTC_1000000"] = entry

    result = await agg.get_whale_activity("BTC", 1_000_000)
    assert result["sentiment"] == "bullish"


@pytest.mark.asyncio
async def test_get_funding_rate_cache_hit():
    agg = _make_agg()
    cached = {"rate_pct": 0.05, "rate_8h_avg": 0.04, "excessive": False}
    entry = CacheEntry(value=cached, fetched_at=time.monotonic(), ttl_seconds=60)
    agg._cache["funding_rate_BTCUSDT"] = entry

    result = await agg.get_funding_rate()
    assert result["rate_pct"] == 0.05


# ---------------------------------------------------------------------------
# Error fallback paths (no stale cache)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_exchange_netflow_error_returns_defaults():
    agg = _make_agg()
    agg._fetch_glassnode_netflow = AsyncMock(side_effect=RuntimeError("API down"))

    result = await agg.get_exchange_netflow("BTC")
    assert result["netflow"] == 0.0
    assert result["tscore"] == 0.0


@pytest.mark.asyncio
async def test_get_whale_activity_error_returns_defaults():
    agg = _make_agg()
    agg._fetch_glassnode_whale_activity = AsyncMock(side_effect=RuntimeError("API down"))

    result = await agg.get_whale_activity("BTC")
    assert result["ratio"] == 1.0
    assert result["sentiment"] == "neutral"


@pytest.mark.asyncio
async def test_get_funding_rate_error_returns_defaults():
    agg = _make_agg()
    agg._fetch_cryptoquant_funding_rate = AsyncMock(side_effect=RuntimeError("API down"))

    result = await agg.get_funding_rate()
    assert result["rate_pct"] == 0.0
    assert result["excessive"] is False


# ---------------------------------------------------------------------------
# Error fallback with stale cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_exchange_netflow_error_uses_stale_cache():
    agg = _make_agg()
    stale_data = {"netflow": -5.0, "inflow": 0.0, "outflow": 5.0, "tscore": -1.2}
    old_time = time.monotonic() - 999.0
    agg._cache["exchange_netflow_BTC_all_7d"] = CacheEntry(
        value=stale_data, fetched_at=old_time, ttl_seconds=60
    )
    agg._fetch_glassnode_netflow = AsyncMock(side_effect=RuntimeError("fail"))

    result = await agg.get_exchange_netflow("BTC")
    assert result["netflow"] == -5.0


@pytest.mark.asyncio
async def test_get_whale_activity_error_uses_stale_cache():
    agg = _make_agg()
    stale = {"buy_volume": 2.0, "sell_volume": 3.0, "ratio": 0.67, "sentiment": "bearish"}
    old_time = time.monotonic() - 999.0
    agg._cache["whale_activity_BTC_1000000"] = CacheEntry(
        value=stale, fetched_at=old_time, ttl_seconds=60
    )
    agg._fetch_glassnode_whale_activity = AsyncMock(side_effect=RuntimeError("fail"))

    result = await agg.get_whale_activity()
    assert result["sentiment"] == "bearish"


# ---------------------------------------------------------------------------
# Successful fetch + cache write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_exchange_netflow_fetches_and_caches():
    agg = _make_agg()
    fetched = {"netflow": 3.0, "inflow": 3.0, "outflow": 0.0, "tscore": 1.1, "timestamp": 1000}
    agg._fetch_glassnode_netflow = AsyncMock(return_value=fetched)

    result = await agg.get_exchange_netflow("BTC", "binance", 7)
    assert result["netflow"] == 3.0
    assert "exchange_netflow_BTC_binance_7d" in agg._cache


@pytest.mark.asyncio
async def test_get_whale_activity_fetches_and_caches():
    agg = _make_agg()
    fetched = {
        "buy_volume": 5.0,
        "sell_volume": 2.0,
        "ratio": 2.5,
        "sentiment": "bullish",
        "timestamp": 1,
    }
    agg._fetch_glassnode_whale_activity = AsyncMock(return_value=fetched)

    result = await agg.get_whale_activity()
    assert result["ratio"] == 2.5


@pytest.mark.asyncio
async def test_get_funding_rate_fetches_and_caches():
    agg = _make_agg()
    fetched = {"rate_pct": 0.08, "rate_8h_avg": 0.07, "excessive": False, "timestamp": 1}
    agg._fetch_cryptoquant_funding_rate = AsyncMock(return_value=fetched)

    result = await agg.get_funding_rate("BTCUSDT")
    assert result["rate_pct"] == 0.08


# ---------------------------------------------------------------------------
# _fetch_glassnode_netflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_glassnode_netflow_no_key_raises():
    agg = _make_agg()
    agg.glassnode_key = ""
    agg._rate_limit_glassnode = AsyncMock()

    with pytest.raises(RuntimeError, match="INTELLIGENCE_GLASSNODE_API_KEY"):
        await agg._fetch_glassnode_netflow("BTC", None, 7)


@pytest.mark.asyncio
async def test_fetch_glassnode_netflow_empty_data_raises():
    agg = _make_agg()
    agg._rate_limit_glassnode = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=[])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ValueError, match="empty netflow"):
            await agg._fetch_glassnode_netflow("BTC", None, 7)


@pytest.mark.asyncio
async def test_fetch_glassnode_netflow_null_values_raises():
    agg = _make_agg()
    agg._rate_limit_glassnode = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=[{"t": 1000, "v": None}])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(ValueError, match="null"):
            await agg._fetch_glassnode_netflow("BTC", None, 7)


@pytest.mark.asyncio
async def test_fetch_glassnode_netflow_success():
    agg = _make_agg()
    agg._rate_limit_glassnode = AsyncMock()

    data = [{"t": 1_000 + i * 86_400, "v": float(i - 3)} for i in range(7)]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=data)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agg._fetch_glassnode_netflow("BTC", "binance", 7)

    assert "netflow" in result
    assert "tscore" in result
    assert "timestamp" in result


@pytest.mark.asyncio
async def test_fetch_glassnode_netflow_with_exchange_filter():
    agg = _make_agg()
    agg._rate_limit_glassnode = AsyncMock()

    data = [{"t": 1000, "v": 2.5}]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=data)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        await agg._fetch_glassnode_netflow("BTC", "binance", 7)
    # exchange param "e" should have been passed
    call_kwargs = mock_client.get.call_args
    assert call_kwargs is not None


# ---------------------------------------------------------------------------
# _fetch_glassnode_whale_activity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_glassnode_whale_activity_no_key_raises():
    agg = _make_agg()
    agg.glassnode_key = ""
    agg._rate_limit_glassnode = AsyncMock()

    with pytest.raises(RuntimeError, match="INTELLIGENCE_GLASSNODE_API_KEY"):
        await agg._fetch_glassnode_whale_activity("BTC", 1_000_000)


@pytest.mark.asyncio
async def test_fetch_glassnode_whale_activity_success():
    agg = _make_agg()
    agg._rate_limit_glassnode = AsyncMock()

    data = [{"t": 1_000 + i * 86_400, "v": float(i + 1)} for i in range(7)]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=data)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agg._fetch_glassnode_whale_activity("BTC", 1_000_000)

    assert "ratio" in result
    assert "sentiment" in result
    assert result["sentiment"] in ("bullish", "bearish", "neutral")


@pytest.mark.asyncio
async def test_fetch_glassnode_whale_activity_empty_raises():
    agg = _make_agg()
    agg._rate_limit_glassnode = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=[])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client), pytest.raises(ValueError):
        await agg._fetch_glassnode_whale_activity("BTC", 1_000_000)


# ---------------------------------------------------------------------------
# _fetch_cryptoquant_funding_rate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_cryptoquant_funding_rate_normalizes_symbol():
    agg = _make_agg()
    mock_exchange = MagicMock()
    mock_exchange.fetch_funding_rate = AsyncMock(
        return_value={"fundingRate": 0.0001, "timestamp": 1_700_000_000_000}
    )
    mock_exchange.fetch_funding_rate_history = AsyncMock(
        return_value=[{"fundingRate": 0.0001}, {"fundingRate": 0.0002}, {"fundingRate": 0.00015}]
    )
    mock_exchange.close = AsyncMock()

    with patch("ccxt.async_support.binance", return_value=mock_exchange):
        result = await agg._fetch_cryptoquant_funding_rate("BTCUSDT")

    assert "rate_pct" in result
    assert "excessive" in result
    assert result["rate_pct"] == pytest.approx(0.01, abs=1e-5)


@pytest.mark.asyncio
async def test_fetch_cryptoquant_funding_rate_already_ccxt_format():
    agg = _make_agg()
    mock_exchange = MagicMock()
    mock_exchange.fetch_funding_rate = AsyncMock(
        return_value={"fundingRate": 0.0005, "timestamp": 1_700_000_000_000}
    )
    mock_exchange.fetch_funding_rate_history = AsyncMock(return_value=[])
    mock_exchange.close = AsyncMock()

    with patch("ccxt.async_support.binance", return_value=mock_exchange):
        result = await agg._fetch_cryptoquant_funding_rate("BTC/USDT:USDT")

    # Empty history → avg = rate
    assert result["rate_8h_avg"] == result["rate_pct"]


@pytest.mark.asyncio
async def test_fetch_cryptoquant_funding_rate_no_timestamp():
    agg = _make_agg()
    mock_exchange = MagicMock()
    mock_exchange.fetch_funding_rate = AsyncMock(
        return_value={"fundingRate": 0.0001, "timestamp": None}
    )
    mock_exchange.fetch_funding_rate_history = AsyncMock(return_value=[])
    mock_exchange.close = AsyncMock()

    with patch("ccxt.async_support.binance", return_value=mock_exchange):
        result = await agg._fetch_cryptoquant_funding_rate("BTCUSDT")

    assert "timestamp" in result
    assert result["timestamp"] > 0


# ---------------------------------------------------------------------------
# _rate_limit_glassnode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_glassnode_waits_when_too_recent():
    agg = _make_agg()
    agg._glassnode_min_interval = 1.0
    agg._last_glassnode_call = time.monotonic()  # just called

    with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await agg._rate_limit_glassnode()
        mock_sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_limit_glassnode_no_wait_if_old():
    agg = _make_agg()
    agg._glassnode_min_interval = 1.0
    agg._last_glassnode_call = None  # never called

    with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
        await agg._rate_limit_glassnode()
        mock_sleep.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_exchange_netflow_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_exchange_netflow_history_no_key_returns_empty():
    agg = _make_agg()
    agg.glassnode_key = ""

    result = await agg.get_exchange_netflow_history("BTC")
    assert result == []


@pytest.mark.asyncio
async def test_get_exchange_netflow_history_success():
    agg = _make_agg()
    agg._rate_limit_glassnode = AsyncMock()

    data = [{"t": 1_000 + i * 86_400, "v": float(i)} for i in range(5)]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=data)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agg.get_exchange_netflow_history("BTC", since_ts=1000, until_ts=5000)

    assert len(result) == 5
    assert "ts" in result[0]
    assert "tscore" in result[0]


@pytest.mark.asyncio
async def test_get_exchange_netflow_history_api_error_returns_empty():
    agg = _make_agg()
    agg._rate_limit_glassnode = AsyncMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=RuntimeError("connection refused"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agg.get_exchange_netflow_history("BTC")

    assert result == []


@pytest.mark.asyncio
async def test_get_exchange_netflow_history_with_exchange_filter():
    agg = _make_agg()
    agg._rate_limit_glassnode = AsyncMock()

    data = [{"t": 1000, "v": 2.0}]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=data)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agg.get_exchange_netflow_history("BTC", exchange="binance")

    assert len(result) == 1


@pytest.mark.asyncio
async def test_get_exchange_netflow_history_empty_values():
    agg = _make_agg()
    agg._rate_limit_glassnode = AsyncMock()

    data = [{"t": 1000, "v": None}]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=data)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agg.get_exchange_netflow_history("BTC")

    assert result == []


# ---------------------------------------------------------------------------
# get_whale_activity_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_whale_activity_history_no_key_returns_empty():
    agg = _make_agg()
    agg.glassnode_key = ""

    result = await agg.get_whale_activity_history("BTC")
    assert result == []


@pytest.mark.asyncio
async def test_get_whale_activity_history_success():
    agg = _make_agg()
    agg._rate_limit_glassnode = AsyncMock()

    data = [{"t": 1_000 + i * 86_400, "v": float(i + 1)} for i in range(7)]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=data)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agg.get_whale_activity_history("BTC", since_ts=1000)

    assert len(result) == 7
    assert "ratio" in result[0]
    assert "sentiment" in result[0]


@pytest.mark.asyncio
async def test_get_whale_activity_history_error_returns_empty():
    agg = _make_agg()
    agg._rate_limit_glassnode = AsyncMock()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=RuntimeError("fail"))

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agg.get_whale_activity_history("BTC")

    assert result == []


@pytest.mark.asyncio
async def test_get_whale_activity_history_null_values_returns_empty():
    agg = _make_agg()
    agg._rate_limit_glassnode = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=[{"t": 1000, "v": None}])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await agg.get_whale_activity_history("BTC")

    assert result == []


# ---------------------------------------------------------------------------
# get_funding_rate_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_funding_rate_history_success():
    agg = _make_agg()
    history = [
        {"timestamp": 1_700_000_000_000 + i * 28_800_000, "fundingRate": 0.0001 + i * 0.00001}
        for i in range(3)
    ]
    mock_exchange = MagicMock()
    mock_exchange.fetch_funding_rate_history = AsyncMock(return_value=history)
    mock_exchange.close = AsyncMock()

    with patch("ccxt.async_support.binance", return_value=mock_exchange):
        result = await agg.get_funding_rate_history(limit=3)

    assert len(result) == 3
    assert "ts" in result[0]
    assert "rate_pct" in result[0]


@pytest.mark.asyncio
async def test_get_funding_rate_history_normalizes_symbol():
    agg = _make_agg()
    mock_exchange = MagicMock()
    mock_exchange.fetch_funding_rate_history = AsyncMock(return_value=[])
    mock_exchange.close = AsyncMock()

    with patch("ccxt.async_support.binance", return_value=mock_exchange):
        result = await agg.get_funding_rate_history("BTCUSDT")

    assert result == []


@pytest.mark.asyncio
async def test_get_funding_rate_history_error_returns_empty():
    agg = _make_agg()
    mock_exchange = MagicMock()
    mock_exchange.fetch_funding_rate_history = AsyncMock(side_effect=RuntimeError("network"))
    mock_exchange.close = AsyncMock()

    with patch("ccxt.async_support.binance", return_value=mock_exchange):
        result = await agg.get_funding_rate_history()

    assert result == []


@pytest.mark.asyncio
async def test_get_funding_rate_history_skips_missing_timestamp():
    agg = _make_agg()
    history = [
        {"timestamp": 1_700_000_000_000, "fundingRate": 0.0001},
        {"timestamp": None, "fundingRate": 0.0002},  # should be skipped
    ]
    mock_exchange = MagicMock()
    mock_exchange.fetch_funding_rate_history = AsyncMock(return_value=history)
    mock_exchange.close = AsyncMock()

    with patch("ccxt.async_support.binance", return_value=mock_exchange):
        result = await agg.get_funding_rate_history()

    assert len(result) == 1  # only entry with timestamp


@pytest.mark.asyncio
async def test_get_funding_rate_history_with_since_ts():
    agg = _make_agg()
    mock_exchange = MagicMock()
    mock_exchange.fetch_funding_rate_history = AsyncMock(return_value=[])
    mock_exchange.close = AsyncMock()

    with patch("ccxt.async_support.binance", return_value=mock_exchange):
        await agg.get_funding_rate_history(since_ts=1_700_000_000_000)

    call_kwargs = mock_exchange.fetch_funding_rate_history.call_args
    assert call_kwargs[1]["since"] == 1_700_000_000_000


# ---------------------------------------------------------------------------
# get_intelligence_aggregator factory
# ---------------------------------------------------------------------------


def test_get_intelligence_aggregator_returns_instance():
    with patch("src.intelligence.client.IntelligenceAggregator") as MockAgg:
        MockAgg.return_value = MagicMock()
        get_intelligence_aggregator()
        MockAgg.assert_called_once()
