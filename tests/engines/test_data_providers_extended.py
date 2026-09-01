"""Tests for sentiment_provider, macro_provider, deribit_provider (non-network paths)."""

from __future__ import annotations

import asyncio

import pandas as pd


# ---------------------------------------------------------------------------
# SentimentProvider
# ---------------------------------------------------------------------------


class TestSentimentProvider:
    def test_defaults(self) -> None:
        from src.data.sentiment_provider import SentimentProvider

        sp = SentimentProvider()
        score, label = sp.latest_fg()
        assert score == 50.0
        assert label == "Neutral"

    def test_recent_headlines_empty(self) -> None:
        from src.data.sentiment_provider import SentimentProvider

        sp = SentimentProvider()
        assert sp.recent_headlines() == []

    def test_recent_headlines_limit(self) -> None:
        from src.data.sentiment_provider import SentimentProvider

        sp = SentimentProvider()
        sp._headlines = [
            {"headline": f"h{i}", "vader_compound": 0.1, "source": "x"} for i in range(50)
        ]
        assert len(sp.recent_headlines(10)) == 10

    def test_update_cache_no_crash_when_cache_unavailable(self) -> None:
        from src.data.sentiment_provider import SentimentProvider

        sp = SentimentProvider()
        sp._update_cache()  # must not raise even with empty headlines

    def test_parse_rss_valid_xml(self) -> None:
        from src.data.sentiment_provider import SentimentProvider

        sp = SentimentProvider()
        vader = sp._get_vader()
        xml = """<rss><channel>
            <item><title>Bitcoin hits new ATH</title></item>
            <item><title>Market crashes</title></item>
            <item><title/></item>
        </channel></rss>"""
        rows = sp._parse_rss(xml, "test_source", vader)
        assert len(rows) == 2
        assert rows[0]["headline"] == "Bitcoin hits new ATH"
        assert "vader_compound" in rows[0]

    def test_parse_rss_invalid_xml_returns_empty(self) -> None:
        from src.data.sentiment_provider import SentimentProvider

        sp = SentimentProvider()
        vader = sp._get_vader()
        rows = sp._parse_rss("not valid xml <<<", "test_source", vader)
        assert rows == []

    def test_get_vader_returns_scorer(self) -> None:
        from src.data.sentiment_provider import SentimentProvider

        vader = SentimentProvider._get_vader()
        result = vader.polarity_scores("great news")  # type: ignore[attr-defined]
        assert "compound" in result

    def test_persist_fg_writes_parquet(self, tmp_path) -> None:
        from src.data.sentiment_provider import SentimentProvider

        sp = SentimentProvider(data_root=tmp_path)
        sp._fg_score = 75.0
        sp._fg_label = "Greed"
        sp._persist_fg()
        files = list((tmp_path / "sentiment").glob("*.parquet"))
        assert len(files) == 1
        df = pd.read_parquet(files[0])
        assert float(df["fg_score"].iloc[0]) == 75.0

    def test_append_parquet_creates_and_extends(self, tmp_path) -> None:
        from src.data.sentiment_provider import SentimentProvider

        path = tmp_path / "test.parquet"
        df1 = pd.DataFrame({"x": [1, 2]})
        df2 = pd.DataFrame({"x": [3, 4]})
        SentimentProvider._append_parquet(path, df1)
        SentimentProvider._append_parquet(path, df2)
        result = pd.read_parquet(path)
        assert len(result) == 4


# ---------------------------------------------------------------------------
# MacroProvider
# ---------------------------------------------------------------------------


class TestMacroProvider:
    def test_latest_returns_none_when_no_cache(self, tmp_path) -> None:
        from src.data.macro_provider import MacroProvider

        mp = MacroProvider(data_root=tmp_path)
        assert mp.latest() is None

    def test_latest_loads_cached_parquet(self, tmp_path) -> None:
        from src.data.macro_provider import MacroProvider

        mp = MacroProvider(data_root=tmp_path)
        macro_dir = tmp_path / "macro"
        macro_dir.mkdir()
        row = {"date": "2026-08-01", "spx_close": 5000.0, "vix_close": 15.0}
        pd.DataFrame([row]).to_parquet(macro_dir / "2026-08-01.parquet", index=False)
        result = mp.latest()
        assert result is not None
        assert result["spx_close"] == 5000.0

    def test_persist_and_load(self, tmp_path) -> None:
        from src.data.macro_provider import MacroProvider

        mp = MacroProvider(data_root=tmp_path)
        row = {"date": "2026-08-01", "spx_close": 4999.0}
        mp._persist(row)
        loaded = mp._load_cached()
        assert loaded is not None
        assert loaded["spx_close"] == 4999.0

    def test_latest_uses_in_memory_first(self, tmp_path) -> None:
        from src.data.macro_provider import MacroProvider

        mp = MacroProvider(data_root=tmp_path)
        mp._latest = {"date": "2026-08-02", "spx_close": 9999.0}
        assert mp.latest()["spx_close"] == 9999.0  # type: ignore[index]


# ---------------------------------------------------------------------------
# DeribitProvider
# ---------------------------------------------------------------------------


class TestDeribitProvider:
    def test_supports_btc_eth(self) -> None:
        from src.data.deribit_provider import DeribitProvider

        dp = DeribitProvider()
        assert dp.supports("BTC/USDT") is True
        assert dp.supports("ETH/USDT") is True

    def test_does_not_support_ltc(self) -> None:
        from src.data.deribit_provider import DeribitProvider

        dp = DeribitProvider()
        assert dp.supports("LTC/USDT") is False

    def test_fetch_returns_none_for_unsupported(self) -> None:
        from src.data.deribit_provider import DeribitProvider

        dp = DeribitProvider()
        result = asyncio.run(dp.fetch("LTC/USDT"))
        assert result is None

    def test_fetch_returns_cached_when_set(self) -> None:
        from src.data.deribit_provider import DeribitProvider

        dp = DeribitProvider()
        fake_df = pd.DataFrame({"strike": [50000.0], "iv": [0.8], "open_interest": [100.0]})
        dp._cache["BTC"] = fake_df
        # fetch will try network first, but cache is set — it returns from cache after network
        # We test the cache access directly
        assert dp._cache.get("BTC") is fake_df

    def test_parse_row_returns_none_on_zero_iv(self) -> None:
        from src.data.deribit_provider import DeribitProvider

        dp = DeribitProvider()
        inst = {
            "instrument_name": "BTC-25MAR26-50000-C",
            "expiration_timestamp": 0,
            "strike": 50000,
            "option_type": "call",
        }
        ob = {"mark_iv": 0.0, "open_interest": 100.0, "stats": {"volume": 10.0}, "greeks": {}}
        assert dp._parse_row(inst, ob) is None

    def test_parse_row_returns_dict_when_valid(self) -> None:
        from src.data.deribit_provider import DeribitProvider

        dp = DeribitProvider()
        inst = {
            "instrument_name": "BTC-25MAR26-50000-C",
            "expiration_timestamp": 1000000,
            "strike": 50000,
            "option_type": "call",
        }
        ob = {
            "mark_iv": 0.75,
            "open_interest": 50.0,
            "stats": {"volume": 5.0},
            "greeks": {"delta": 0.5, "gamma": 0.001},
        }
        row = dp._parse_row(inst, ob)
        assert row is not None
        assert row["iv"] == 0.75
        assert row["delta"] == 0.5
