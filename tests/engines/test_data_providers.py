"""Tests for CAT-1 data providers and quality gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from src.data.quality_gate import DataQualityGate


# ---------------------------------------------------------------------------
# Quality Gate
# ---------------------------------------------------------------------------


class TestDataQualityGate:
    def setup_method(self):
        self.gate = DataQualityGate()

    def _make_ohlcv(self, n: int = 5, age_seconds: int = 0) -> pd.DataFrame:
        now = datetime.now(UTC) - timedelta(seconds=age_seconds)
        ts = [now - timedelta(hours=i) for i in reversed(range(n))]
        prices = np.linspace(50000, 50100, n)
        return pd.DataFrame(
            {
                "timestamp_utc": ts,
                "close": prices,
                "volume": np.ones(n) * 1000,
            }
        )

    def test_ohlcv_fresh_passes(self):
        df = self._make_ohlcv()
        result = self.gate.check_ohlcv(df)
        assert result.passed

    def test_ohlcv_stale_rejects(self):
        df = self._make_ohlcv(age_seconds=400)  # >5 min stale
        result = self.gate.check_ohlcv(df)
        assert not result.passed
        assert "stale" in result.reason

    def test_ohlcv_extreme_return_rejects(self):
        df = self._make_ohlcv()
        df.loc[df.index[-1], "close"] = 100_000.0  # 100% jump
        result = self.gate.check_ohlcv(df)
        assert not result.passed
        assert "extreme_return" in result.reason

    def test_ohlcv_consecutive_zero_volume_rejects(self):
        df = self._make_ohlcv(n=5)
        df["volume"] = 0.0
        result = self.gate.check_ohlcv(df)
        assert not result.passed

    def test_orderbook_wide_spread_rejects(self):
        result = self.gate.check_orderbook(spread_bps=250.0)
        assert not result.passed

    def test_orderbook_normal_spread_passes(self):
        result = self.gate.check_orderbook(spread_bps=5.0)
        assert result.passed

    def test_options_zero_iv_rejects(self):
        result = self.gate.check_options_row(iv=0.0, oi=1000.0)
        assert not result.passed

    def test_options_zero_oi_rejects(self):
        result = self.gate.check_options_row(iv=0.5, oi=0.0)
        assert not result.passed

    def test_options_valid_passes(self):
        result = self.gate.check_options_row(iv=0.8, oi=500.0)
        assert result.passed

    def test_macro_fresh_passes(self):
        row = {"date": datetime.now(UTC).date().isoformat()}
        result = self.gate.check_macro(row)
        assert result.passed

    def test_macro_stale_rejects(self):
        old_date = (datetime.now(UTC) - timedelta(days=5)).date().isoformat()
        result = self.gate.check_macro({"date": old_date})
        assert not result.passed

    def test_price_deviation_within_tolerance_passes(self):
        result = self.gate.check_price_deviation(50000.0, 50200.0)
        assert result.passed  # 0.4% < 0.5%

    def test_price_deviation_exceeds_tolerance_rejects(self):
        result = self.gate.check_price_deviation(50000.0, 50400.0)
        assert not result.passed  # 0.8% > 0.5%


# ---------------------------------------------------------------------------
# OrderbookStream (unit — no WebSocket)
# ---------------------------------------------------------------------------


def test_orderbook_stream_flush(tmp_path):
    import json

    from src.data.orderbook_stream import OrderbookSnapshot, OrderbookStream

    stream = OrderbookStream(symbol="btcusdt", data_root=tmp_path)
    for _i in range(5):
        stream._snapshots.append(
            OrderbookSnapshot(
                timestamp_utc=datetime.now(UTC),
                bids_json=json.dumps([["50000", "1.0"]]),
                asks_json=json.dumps([["50001", "1.0"]]),
                mid=50000.5,
                spread_bps=2.0,
            )
        )
    stream._flush_orderbook()
    out = tmp_path / "orderbook" / "btcusdt"
    files = list(out.glob("*.parquet"))
    assert len(files) == 1
    df = pd.read_parquet(files[0])
    assert len(df) == 5
    assert "mid" in df.columns


def test_orderbook_quality_rejects_wide_spread():
    from src.data.orderbook_stream import OrderbookStream

    stream = OrderbookStream(symbol="btcusdt")

    captured = []
    stream._snapshots = captured

    stream._handle_depth(
        {
            "bids": [["50000", "1.0"]],
            "asks": [["51100", "1.0"]],  # spread > 200 bps
        }
    )
    assert len(captured) == 0
