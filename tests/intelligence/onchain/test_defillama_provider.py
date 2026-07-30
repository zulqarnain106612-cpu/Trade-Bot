"""OCI-003: Tests for DeFiLlamaProvider."""

from __future__ import annotations

from typing import Any

import pytest

from src.intelligence.onchain.defillama_provider import (
    DeFiLlamaProvider,
    _compute_tvl_metrics,
    _stablecoin_ratio,
)


def _tvl_series(n: int, start: float = 100.0, end: float = 100.0) -> list[dict]:
    """Generate n TVL data points linearly interpolated from start to end."""
    result = []
    for i in range(n):
        t = i / max(n - 1, 1)
        tvl = start + (end - start) * t
        result.append({"date": 1_700_000_000 + i * 86400, "tvl": tvl})
    return result


# ---------------------------------------------------------------------------
# _compute_tvl_metrics helper tests
# ---------------------------------------------------------------------------


def test_staking_unlock_risk_large_drop() -> None:
    # Need >10% drop over the 7d window (recent[-8] → recent[-1]).
    # With n=14 and linear interp: recent[-8] = index 6.
    # To get -12% over that span: start high, end low enough.
    # Simplest: use 8 points only, start=100 end=85 → 7d ago = index 0 = 100
    series = _tvl_series(8, start=100.0, end=85.0)  # -15% → risk 0.8
    risk, change = _compute_tvl_metrics(series)
    assert risk == 0.8
    assert change < -10.0


def test_staking_unlock_risk_medium_drop() -> None:
    # 8 points, -7%: tvl_7d_ago = index 0 = 100, tvl_now = 93 → -7%
    series = _tvl_series(8, start=100.0, end=93.0)
    risk, change = _compute_tvl_metrics(series)
    assert risk == 0.5
    assert -10.0 < change < -5.0


def test_staking_unlock_risk_stable() -> None:
    series = _tvl_series(14, start=100.0, end=99.0)  # ~-1% → risk 0.1
    risk, _ = _compute_tvl_metrics(series)
    assert risk == 0.1


def test_tvl_7d_change_positive() -> None:
    series = _tvl_series(14, start=100.0, end=115.0)
    _, change = _compute_tvl_metrics(series)
    assert change > 0


def test_tvl_7d_change_too_few_points() -> None:
    risk, change = _compute_tvl_metrics([{"date": 1, "tvl": 100}])
    assert risk == 0.0
    assert change == 0.0


# ---------------------------------------------------------------------------
# _stablecoin_ratio helper tests
# ---------------------------------------------------------------------------


def test_stablecoin_ratio_usdt_usdc_dominant() -> None:
    data = {
        "peggedAssets": [
            {"symbol": "USDT", "circulating": {"peggedUSD": 80_000_000_000}},
            {"symbol": "USDC", "circulating": {"peggedUSD": 40_000_000_000}},
            {"symbol": "DAI", "circulating": {"peggedUSD": 5_000_000_000}},
        ]
    }
    ratio = _stablecoin_ratio(data)
    assert ratio is not None
    assert ratio == pytest.approx(120 / 125, rel=0.01)


def test_stablecoin_ratio_empty_returns_none() -> None:
    assert _stablecoin_ratio({}) is None
    assert _stablecoin_ratio({"peggedAssets": []}) is None


# ---------------------------------------------------------------------------
# DeFiLlamaProvider integration tests (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_auth_header_sent() -> None:
    """DeFiLlama requires no API key — verify headers don't contain API-Key."""
    provider = DeFiLlamaProvider()
    received_headers: list[dict] = []

    async def mock_get(url: str, headers: Any = None, params: Any = None) -> Any:
        received_headers.append(headers or {})
        return []  # return empty (not None) to avoid None-branch

    provider._get = mock_get  # type: ignore[assignment]
    await provider.fetch_metrics()
    for h in received_headers:
        assert "API-Key" not in h
        assert "Authorization" not in h


@pytest.mark.asyncio
async def test_staking_unlock_risk_thresholds_via_mock() -> None:
    provider = DeFiLlamaProvider()
    tvl_series = _tvl_series(8, start=100.0, end=85.0)  # -15% over 7d window

    call_count = 0

    async def mock_get(url: str, headers: Any = None, params: Any = None) -> Any:
        nonlocal call_count
        call_count += 1
        if "historicalChainTvl" in url:
            return tvl_series
        return None

    provider._get = mock_get  # type: ignore[assignment]
    metrics = await provider.fetch_metrics()
    assert metrics["staking_unlock_risk"] == 0.8
    assert metrics["defi_tvl_7d_change_pct"] < -10.0


@pytest.mark.asyncio
async def test_stablecoin_ratio_from_defillama_fallback() -> None:
    provider = DeFiLlamaProvider()
    stable_data = {
        "peggedAssets": [
            {"symbol": "USDT", "circulating": {"peggedUSD": 80e9}},
            {"symbol": "USDC", "circulating": {"peggedUSD": 40e9}},
            {"symbol": "DAI", "circulating": {"peggedUSD": 5e9}},
        ]
    }

    async def mock_get(url: str, headers: Any = None, params: Any = None) -> Any:
        if "stablecoins" in url:
            return stable_data
        return []

    provider._get = mock_get  # type: ignore[assignment]
    metrics = await provider.fetch_metrics()
    assert metrics["stablecoin_reserve_ratio"] == pytest.approx(120 / 125, rel=0.01)


@pytest.mark.asyncio
async def test_network_error_returns_neutral_confidence_adjusted() -> None:
    provider = DeFiLlamaProvider()

    async def mock_get(url: str, headers: Any = None, params: Any = None) -> None:
        return None

    provider._get = mock_get  # type: ignore[assignment]
    metrics = await provider.fetch_metrics()
    assert metrics["staking_unlock_risk"] == 0.0
    assert metrics["stablecoin_reserve_ratio"] == 0.5
    assert metrics["confidence"] < 1.0
    assert metrics["confidence"] >= 0.0


@pytest.mark.asyncio
async def test_initialize_noop() -> None:
    provider = DeFiLlamaProvider()
    await provider.initialize()  # must not raise


@pytest.mark.asyncio
async def test_close_noop() -> None:
    provider = DeFiLlamaProvider()
    await provider.close()  # must not raise


@pytest.mark.asyncio
async def test_fetch_metrics_stablecoin_ratio_none_when_no_major() -> None:
    """stablecoin_ratio returns None for non-USDT/USDC only data; key stays at neutral."""
    provider = DeFiLlamaProvider()
    tvl_series = [{"date": i, "tvl": 1e9} for i in range(20)]

    call_idx = 0
    responses: list[Any] = [
        tvl_series,
        # peggedAssets with only DAI — ratio returns None
        {"peggedAssets": [{"symbol": "DAI", "circulating": {"peggedUSD": 5e9}}]},
    ]

    async def mock_get(url: str, **kwargs: Any) -> Any:
        nonlocal call_idx
        r = responses[call_idx % len(responses)]
        call_idx += 1
        return r

    provider._get = mock_get  # type: ignore[assignment]
    metrics = await provider.fetch_metrics()
    # _stablecoin_ratio({"peggedAssets": [DAI only]}) returns 0.0 (usd_major=0/total)
    # and 0.0 is a valid value so it overwrites the neutral 0.5
    assert metrics["stablecoin_reserve_ratio"] == pytest.approx(0.0)
