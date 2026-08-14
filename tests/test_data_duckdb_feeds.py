"""Tests for DuckDBStore and RedpandaFeeds/Consumer."""

from __future__ import annotations

import asyncio


# ─── DuckDBStore ──────────────────────────────────────────────────────────────


class TestDuckDBStore:
    def test_init_in_memory(self) -> None:
        from src.data.duckdb_store import DuckDBStore

        store = DuckDBStore(path=None)
        assert store is not None
        store.close()

    def test_init_on_disk(self, tmp_path) -> None:
        from src.data.duckdb_store import DuckDBStore

        db_path = tmp_path / "test.duckdb"
        store = DuckDBStore(path=db_path)
        assert db_path.exists()
        store.close()

    def test_write_ohlcv_single_row(self) -> None:
        from src.data.duckdb_store import DuckDBStore

        store = DuckDBStore(path=None)
        row = {
            "symbol": "BTC/USDT",
            "timeframe": "1m",
            "ts": 1700000000000,
            "open": 50000.0,
            "high": 50100.0,
            "low": 49900.0,
            "close": 50050.0,
            "volume": 10.0,
        }
        store.write_ohlcv([row])
        store.close()

    def test_write_ohlcv_empty_no_crash(self) -> None:
        from src.data.duckdb_store import DuckDBStore

        store = DuckDBStore(path=None)
        store.write_ohlcv([])
        store.close()

    def test_write_horizon_metric(self) -> None:
        from src.data.duckdb_store import DuckDBStore

        store = DuckDBStore(path=None)
        store.write_horizon_metric(
            horizon_id=0,
            label="30s",
            sharpe=1.5,
            confidence=0.8,
            direction=1,
            drift_detected=False,
        )
        store.close()

    def test_write_ecc_signal(self) -> None:
        from src.data.duckdb_store import DuckDBStore

        store = DuckDBStore(path=None)
        store.write_ecc_signal(
            {
                "cluster_flow_score": 0.3,
                "ecdsa_weakness_score": 0.1,
                "hodler_index": 0.7,
            }
        )
        store.close()

    def test_write_feature_log(self) -> None:
        from src.data.duckdb_store import DuckDBStore

        store = DuckDBStore(path=None)
        store.write_feature_log("BTC/USDT", {"ofi": 0.5, "vpin": 0.3, "kyle_lambda": 0.001})
        store.close()

    def test_query_ohlcv_empty(self) -> None:
        from src.data.duckdb_store import DuckDBStore

        store = DuckDBStore(path=None)
        df = store.query_ohlcv("BTC/USDT", "1m")
        assert hasattr(df, "columns")
        store.close()

    def test_query_horizon_history_empty(self) -> None:
        from src.data.duckdb_store import DuckDBStore

        store = DuckDBStore(path=None)
        df = store.query_horizon_history(horizon_id=0)
        assert hasattr(df, "columns")
        store.close()

    def test_query_ecc_history_empty(self) -> None:
        from src.data.duckdb_store import DuckDBStore

        store = DuckDBStore(path=None)
        df = store.query_ecc_history()
        assert hasattr(df, "columns")
        store.close()

    def test_roundtrip_ohlcv(self) -> None:
        from src.data.duckdb_store import DuckDBStore

        store = DuckDBStore(path=None)
        row = {
            "symbol": "ETH/USDT",
            "timeframe": "5m",
            "ts": 1700000060000,
            "open": 3000.0,
            "high": 3010.0,
            "low": 2990.0,
            "close": 3005.0,
            "volume": 50.0,
        }
        store.write_ohlcv([row])
        df = store.query_ohlcv("ETH/USDT", "5m", limit=10)
        assert len(df) == 1
        assert float(df["close"].iloc[0]) == 3005.0
        store.close()


# ─── RedpandaFeeds ────────────────────────────────────────────────────────────


class TestRedpandaFeeds:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_init_no_crash(self) -> None:
        from src.data.feeds import RedpandaFeeds

        feeds = RedpandaFeeds()
        assert feeds is not None

    def test_start_without_broker_does_not_crash(self) -> None:
        from src.data.feeds import RedpandaFeeds

        feeds = RedpandaFeeds()
        self._run(feeds.start())

    def test_stop_before_start_is_safe(self) -> None:
        from src.data.feeds import RedpandaFeeds

        feeds = RedpandaFeeds()
        self._run(feeds.stop())

    def test_publish_ohlcv_no_broker_no_crash(self) -> None:
        from src.data.feeds import RedpandaFeeds

        feeds = RedpandaFeeds()
        bar = {"open": 50000.0, "high": 50100.0, "low": 49900.0, "close": 50050.0, "volume": 1.0}
        self._run(feeds.publish_ohlcv("BTC/USDT", "1m", bar))

    def test_publish_orderbook_no_broker_no_crash(self) -> None:
        from src.data.feeds import RedpandaFeeds

        feeds = RedpandaFeeds()
        ob = {"bids": [[49990.0, 1.0]], "asks": [[50010.0, 1.0]]}
        self._run(feeds.publish_orderbook("BTC/USDT", ob))

    def test_publish_ecc_no_broker_no_crash(self) -> None:
        from src.data.feeds import RedpandaFeeds

        feeds = RedpandaFeeds()
        self._run(feeds.publish_ecc({"cluster_flow_score": 0.5}))
