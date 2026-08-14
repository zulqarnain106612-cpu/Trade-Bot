"""Tests for ProviderCache singleton."""

from __future__ import annotations

import pandas as pd
import pytest

from src.data.provider_cache import _ProviderCache, get_provider_cache


class TestProviderCache:
    def setup_method(self):
        self.cache = _ProviderCache()

    def test_sentiment_roundtrip(self):
        self.cache.set_sentiment(42.0, "Fear", -0.3)
        s = self.cache.get_sentiment()
        assert s is not None
        assert s["fg_score"] == 42.0
        assert s["fg_label"] == "Fear"
        assert s["vader_compound"] == -0.3

    def test_macro_roundtrip(self):
        self.cache.set_macro({"spx": 5000.0, "vix": 18.5})
        m = self.cache.get_macro()
        assert m is not None
        assert m["spx"] == 5000.0

    def test_options_roundtrip(self):
        df = pd.DataFrame([{"iv": 0.8, "oi": 1000}])
        self.cache.set_options("BTC", df)
        result = self.cache.get_options("BTC")
        assert result is not None
        assert len(result) == 1

    def test_orderbook_roundtrip(self):
        df = pd.DataFrame([{"mid": 50000.0, "spread_bps": 2.0}])
        self.cache.set_orderbook("BTC/USDT", df)
        result = self.cache.get_orderbook("BTC/USDT")
        assert result is not None

    def test_exchange_flows_default_empty(self):
        assert self.cache.get_exchange_flows() == []

    def test_exchange_flows_roundtrip(self):
        flows = [{"from": "binance", "to": "okx", "amount": 100.0}]
        self.cache.set_exchange_flows(flows)
        assert self.cache.get_exchange_flows() == flows

    def test_snapshot_contains_all_keys(self):
        snap = self.cache.snapshot("BTC/USDT")
        assert "sentiment" in snap
        assert "macro" in snap
        assert "options" in snap
        assert "orderbook" in snap
        assert "exchange_flows" in snap

    def test_snapshot_missing_keys_return_none_or_empty(self):
        snap = self.cache.snapshot("ETH/USDT")
        assert snap["sentiment"] is None
        assert snap["macro"] is None
        assert snap["exchange_flows"] == []

    def test_onchain_roundtrip(self):
        self.cache.set_onchain({"tvl_24h_change_pct": 0.03})
        result = self.cache.get_onchain()
        assert result is not None
        assert result["tvl_24h_change_pct"] == pytest.approx(0.03)

    def test_get_provider_cache_is_singleton(self):
        c1 = get_provider_cache()
        c2 = get_provider_cache()
        assert c1 is c2
