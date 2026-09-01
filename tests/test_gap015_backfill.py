"""
GAP-015 backfill pipeline tests.

Covers:
  - storage migration v3 creates intelligence_features_history table
  - store_intelligence_features / fetch_intelligence_features round-trip
  - intelligence_feature_coverage accuracy
  - get_active_feature_columns coverage gating
  - IntelligenceAggregator.get_funding_rate_history graceful absent-key path
  - IntelligenceAggregator.get_exchange_netflow_history graceful absent-key path
  - IntelligenceAggregator.get_whale_activity_history graceful absent-key path
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data.storage import StorageBackend
from src.features.intelligence_features import INTELLIGENCE_FEATURE_COLUMNS
from src.features.pipeline import (
    BASE_FEATURE_COLUMNS,
    FEATURE_COLUMNS,
    get_active_feature_columns,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def storage(tmp_db):
    return StorageBackend(db_path=tmp_db)


async def _init(storage: StorageBackend) -> StorageBackend:
    await storage.initialize()
    return storage


# ---------------------------------------------------------------------------
# Migration v3 / schema tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_creates_table(storage):
    """intelligence_features_history table and index must exist after init."""
    await storage.initialize()
    conn = storage._require_conn()
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='intelligence_features_history'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None, "intelligence_features_history table not created by migration v3"

    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_intel_hist_ts'"
    ) as cur:
        idx = await cur.fetchone()
    assert idx is not None, "idx_intel_hist_ts index not created"


@pytest.mark.asyncio
async def test_schema_version_matches_current(storage):
    from src.data.storage import _SCHEMA_VERSION

    await storage.initialize()
    conn = storage._require_conn()
    async with conn.execute("PRAGMA user_version") as cur:
        row = await cur.fetchone()
    assert int(row[0]) == _SCHEMA_VERSION


# ---------------------------------------------------------------------------
# store / fetch round-trip
# ---------------------------------------------------------------------------

_SAMPLE_FEATURES = {
    "intelligence_binance_funding_rate_pct": 0.012,
    "intelligence_exchange_netflow_7d_zscore": -1.23,
    "intelligence_whale_buy_sell_ratio": 1.05,
    # rest are None (not yet fetched)
}


@pytest.mark.asyncio
async def test_store_and_fetch_roundtrip(storage):
    await storage.initialize()
    bar_ts = 1_700_000_000_000  # arbitrary Unix ms

    await storage.store_intelligence_features(
        symbol="BTCUSDT",
        timeframe="1h",
        bar_ts=bar_ts,
        features=_SAMPLE_FEATURES,
        confidence=0.2,
        source="test",
    )

    df = await storage.fetch_intelligence_features("BTCUSDT", "1h")
    assert not df.empty
    assert bar_ts in df.index

    row = df.loc[bar_ts]
    assert abs(row["intelligence_binance_funding_rate_pct"] - 0.012) < 1e-6
    assert abs(row["intelligence_exchange_netflow_7d_zscore"] - (-1.23)) < 1e-6
    assert abs(row["intelligence_confidence"] - 0.2) < 1e-6


@pytest.mark.asyncio
async def test_store_upsert_replaces(storage):
    """INSERT OR REPLACE — second write overwrites first."""
    await storage.initialize()
    bar_ts = 1_700_000_001_000

    await storage.store_intelligence_features(
        "BTCUSDT",
        "1h",
        bar_ts,
        {"intelligence_binance_funding_rate_pct": 0.01},
        confidence=0.067,
    )
    await storage.store_intelligence_features(
        "BTCUSDT",
        "1h",
        bar_ts,
        {"intelligence_binance_funding_rate_pct": 0.05},
        confidence=0.067,
    )

    df = await storage.fetch_intelligence_features("BTCUSDT", "1h")
    assert abs(df.loc[bar_ts]["intelligence_binance_funding_rate_pct"] - 0.05) < 1e-6


@pytest.mark.asyncio
async def test_fetch_empty_returns_empty_df(storage):
    await storage.initialize()
    df = await storage.fetch_intelligence_features("BTCUSDT", "1h")
    assert df.empty


# ---------------------------------------------------------------------------
# Coverage tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coverage_no_rows_returns_zero_total(storage):
    """No rows for symbol/timeframe → total_rows=0 and empty coverage dict."""
    await storage.initialize()
    cov = await storage.intelligence_feature_coverage("BTCUSDT", "1h")
    assert cov == {"total_rows": 0, "coverage": {}}


@pytest.mark.asyncio
async def test_coverage_all_null(storage):
    """Row with all-NULL features → coverage 0 for all columns."""
    await storage.initialize()
    await storage.store_intelligence_features(
        "BTCUSDT", "1h", 1_700_000_002_000, {}, confidence=0.0
    )
    cov = await storage.intelligence_feature_coverage("BTCUSDT", "1h")
    assert cov["total_rows"] == 1
    for col, frac in cov["coverage"].items():
        assert frac == 0.0, f"{col} should be 0% covered"


@pytest.mark.asyncio
async def test_coverage_partial(storage):
    """Only funding_rate_pct set → only that column has 100% coverage."""
    await storage.initialize()
    for i in range(10):
        await storage.store_intelligence_features(
            "BTCUSDT",
            "1h",
            1_700_000_000_000 + i * 3_600_000,
            {"intelligence_binance_funding_rate_pct": 0.01 * i},
            confidence=1 / 15,
        )

    cov = await storage.intelligence_feature_coverage("BTCUSDT", "1h")
    assert cov["total_rows"] == 10
    assert cov["coverage"]["intelligence_binance_funding_rate_pct"] == 1.0
    assert cov["coverage"]["intelligence_exchange_netflow_7d_zscore"] == 0.0


# ---------------------------------------------------------------------------
# get_active_feature_columns tests
# ---------------------------------------------------------------------------


_N_BASE = len(BASE_FEATURE_COLUMNS)  # 8 after adding garch_vol_forecast
_N_INTEL = 18


def test_no_coverage_returns_base():
    cols = get_active_feature_columns(None)
    assert cols == list(BASE_FEATURE_COLUMNS)
    assert len(cols) == _N_BASE


def test_empty_coverage_returns_base():
    cols = get_active_feature_columns({})
    assert cols == list(BASE_FEATURE_COLUMNS)


def test_full_coverage_returns_25():
    full = dict.fromkeys(INTELLIGENCE_FEATURE_COLUMNS, 1.0)
    cols = get_active_feature_columns(full, min_coverage=0.6)
    assert len(cols) == _N_BASE + _N_INTEL  # 8 base + 18 intel (OCI-012)
    assert cols[:_N_BASE] == list(BASE_FEATURE_COLUMNS)
    for ic in INTELLIGENCE_FEATURE_COLUMNS:
        assert ic in cols


def test_partial_coverage_excludes_low_cols():
    partial = {c: (0.9 if i < 3 else 0.1) for i, c in enumerate(INTELLIGENCE_FEATURE_COLUMNS)}
    cols = get_active_feature_columns(partial, min_coverage=0.6)
    # 8 base + 3 high-coverage intel
    assert len(cols) == _N_BASE + 3
    assert cols[:_N_BASE] == list(BASE_FEATURE_COLUMNS)


def test_threshold_boundary():
    # Exactly at threshold → included
    cov = dict.fromkeys(INTELLIGENCE_FEATURE_COLUMNS, 0.6)
    cols = get_active_feature_columns(cov, min_coverage=0.6)
    assert len(cols) == _N_BASE + _N_INTEL

    # Just below → excluded
    cov_below = dict.fromkeys(INTELLIGENCE_FEATURE_COLUMNS, 0.5999)
    cols_below = get_active_feature_columns(cov_below, min_coverage=0.6)
    assert len(cols_below) == _N_BASE


def test_feature_columns_backward_compat():
    """FEATURE_COLUMNS alias still equals BASE_FEATURE_COLUMNS."""
    assert FEATURE_COLUMNS == BASE_FEATURE_COLUMNS
    # Immutable: this is the feature contract every model is trained against,
    # and the two names share one object.
    assert isinstance(BASE_FEATURE_COLUMNS, tuple)


# ---------------------------------------------------------------------------
# IntelligenceAggregator historical methods — no-key graceful path
# ---------------------------------------------------------------------------


class _FakeSettings:
    glassnode_api_key = ""
    cryptoquant_api_key = ""
    cache_ttl_onchain_seconds = 3600
    cache_ttl_exchange_seconds = 300
    glassnode_base_url = "https://api.glassnode.com/v1/metrics"
    glassnode_rate_limit_seconds = 0.0
    funding_rate_perp_symbol = "BTCUSDT"


@pytest.fixture
def aggregator_no_keys():
    from src.intelligence.client import IntelligenceAggregator

    return IntelligenceAggregator(_settings=_FakeSettings())


@pytest.mark.asyncio
async def test_netflow_history_no_key_returns_empty(aggregator_no_keys):
    result = await aggregator_no_keys.get_exchange_netflow_history()
    assert result == []


@pytest.mark.asyncio
async def test_whale_history_no_key_returns_empty(aggregator_no_keys):
    result = await aggregator_no_keys.get_whale_activity_history()
    assert result == []


@pytest.mark.asyncio
async def test_funding_rate_history_no_key_uses_public_binance(aggregator_no_keys):
    """Funding rate history uses Binance public API — should not fail due to missing key."""
    mock_history = [
        {"timestamp": 1_700_000_000_000, "fundingRate": 0.0001},
        {"timestamp": 1_700_028_800_000, "fundingRate": 0.00012},
    ]

    with patch("ccxt.async_support.binance") as mock_cls:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_funding_rate_history = AsyncMock(return_value=mock_history)
        mock_exchange.close = AsyncMock()
        mock_cls.return_value = mock_exchange

        result = await aggregator_no_keys.get_funding_rate_history(since_ts=0, limit=10)

    assert len(result) == 2
    assert abs(result[0]["rate_pct"] - 0.01) < 1e-6  # 0.0001 * 100
    assert result[0]["ts"] == 1_700_000_000_000


@pytest.mark.asyncio
async def test_funding_rate_history_exchange_error_returns_empty(aggregator_no_keys):
    with patch("ccxt.async_support.binance") as mock_cls:
        mock_exchange = AsyncMock()
        mock_exchange.fetch_funding_rate_history = AsyncMock(side_effect=Exception("network error"))
        mock_exchange.close = AsyncMock()
        mock_cls.return_value = mock_exchange

        result = await aggregator_no_keys.get_funding_rate_history()

    assert result == []


# ---------------------------------------------------------------------------
# Historical methods with Glassnode key — mock HTTP path
# ---------------------------------------------------------------------------


class _FakeSettingsWithKey:
    glassnode_api_key = "test-key-abc"
    cryptoquant_api_key = ""
    cache_ttl_onchain_seconds = 3600
    cache_ttl_exchange_seconds = 300
    glassnode_base_url = "https://api.glassnode.com/v1/metrics"
    glassnode_rate_limit_seconds = 0.0
    funding_rate_perp_symbol = "BTCUSDT"


@pytest.fixture
def aggregator_with_key():
    from src.intelligence.client import IntelligenceAggregator

    return IntelligenceAggregator(_settings=_FakeSettingsWithKey())


@pytest.mark.asyncio
async def test_netflow_history_with_key_parses_response(aggregator_with_key):
    mock_data = [
        {"t": 1_700_000_000, "v": 1000.0},
        {"t": 1_700_086_400, "v": -500.0},
        {"t": 1_700_172_800, "v": 200.0},
    ]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = mock_data

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await aggregator_with_key.get_exchange_netflow_history(
            symbol="BTC", since_ts=1_700_000_000, until_ts=1_700_172_800
        )

    assert len(result) == 3
    # Sorted ascending by ts
    assert result[0]["ts"] == 1_700_000_000
    assert result[1]["ts"] == 1_700_086_400
    # tscore: value -500 is below mean (233.33), should be negative
    assert result[1]["tscore"] < 0
    for r in result:
        assert "ts" in r and "netflow" in r and "tscore" in r


@pytest.mark.asyncio
async def test_whale_history_with_key_parses_ratio(aggregator_with_key):
    # Increasing volume pattern → later bars should have ratio > 1
    mock_data = [{"t": 1_700_000_000 + i * 86_400, "v": float(1000 + i * 500)} for i in range(8)]
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = mock_data

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await aggregator_with_key.get_whale_activity_history(symbol="BTC")

    assert len(result) == 8
    for r in result:
        assert "ts" in r and "ratio" in r and "sentiment" in r
        assert 0.1 <= r["ratio"] <= 10.0
    # Later bars (higher volume) should trend bullish
    assert result[-1]["ratio"] >= result[0]["ratio"]
