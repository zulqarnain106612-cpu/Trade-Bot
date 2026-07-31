"""
Regression tests for two bugs found and fixed this session (GAP-015 follow-on):

1. BinanceIntelligenceProvider._fetch_whale_taker_ratio() unconditionally
   returned the hardcoded neutral 1.0, because it read index 7 of ccxt's
   *normalized* fetch_ohlcv() output (always exactly 6 fields -- ccxt strips
   taker-buy-volume), and even the raw-format index used (7) was wrong
   (Binance's raw kline schema has taker_buy_base_asset_volume at index 9,
   not 7 -- index 7 is quote_asset_volume). Fixed by calling the raw/implicit
   ccxt endpoint (fapiPublicGetKlines) directly and reading index 9.

2. IntelligenceAnalyzer.compute_metrics() hardcoded 11 of 15 metrics to
   plausible-looking constants (e.g. exchange_reserve_ratio=0.35) marked
   only by a `# TODO` comment, while `confidence` was never penalized for
   any of them -- so it could report confidence~=1.0 while most of the
   payload was fabricated. Fixed: unimplemented fields are now NaN, and
   confidence is capped by the real fraction of implemented metrics.
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.intelligence.metrics import IntelligenceAnalyzer
from src.intelligence.providers.binance_provider import BinanceIntelligenceProvider


# ---------------------------------------------------------------------------
# Bug 1: whale taker-ratio ccxt-normalization fix
# ---------------------------------------------------------------------------


class TestWhaleTakerRatioFix:
    def _make_provider(self) -> BinanceIntelligenceProvider:
        p = BinanceIntelligenceProvider(symbol="BTC/USDT", perp_symbol="BTC/USDT:USDT")
        # Bypass initialize()/load_markets(); stub just enough of the ccxt
        # perp instance for market() + the raw klines call.
        p._perp = MagicMock()
        p._perp.market.return_value = {"id": "BTCUSDT"}
        return p

    @pytest.mark.asyncio
    async def test_reads_real_taker_buy_volume_not_hardcoded_neutral(self):
        """
        Regression guard: with a raw-kline fixture where taker-buy volume is
        clearly skewed (80% of total volume is taker-buy), the computed
        ratio must reflect that skew -- NOT silently return 1.0.

        Fixture shape matches Binance's documented raw kline schema:
        [open_time, open, high, low, close, volume, close_time,
         quote_asset_volume, number_of_trades, taker_buy_base_asset_volume,
         taker_buy_quote_asset_volume, ignore]
        """
        provider = self._make_provider()
        raw_klines = [
            [
                1782889200000,
                "100",
                "101",
                "99",
                "100.5",
                "1000",
                1782890099999,
                "100500",
                "500",
                "800",  # taker_buy_base_asset_volume -- index 9, 80% of volume
                "80400",
                "0",
            ],
            [
                1782890100000,
                "100.5",
                "102",
                "100",
                "101",
                "1000",
                1782890999999,
                "101000",
                "500",
                "800",
                "80800",
                "0",
            ],
        ]
        provider._perp.fapiPublicGetKlines = AsyncMock(return_value=raw_klines)

        ratio = await provider._fetch_whale_taker_ratio()

        # total_vol=2000, taker_buy=1600, sell=400 -> ratio = 1600/400 = 4.0
        assert ratio == pytest.approx(4.0, rel=1e-6)
        assert ratio != 1.0  # must not be the old hardcoded neutral fallback

    @pytest.mark.asyncio
    async def test_calls_raw_endpoint_not_normalized_fetch_ohlcv(self):
        """The fix must bypass ccxt's normalized fetch_ohlcv (which strips
        taker-buy-volume) and call the raw implicit method instead."""
        provider = self._make_provider()
        provider._perp.fapiPublicGetKlines = AsyncMock(
            return_value=[
                [0, "1", "1", "1", "1", "100", 0, "100", "1", "50", "50", "0"],
            ]
        )
        # If the implementation regresses to fetch_ohlcv(), this would need
        # to exist; assert it's simply never called.
        provider._perp.fetch_ohlcv = AsyncMock(
            side_effect=AssertionError("must not call normalized fetch_ohlcv")
        )

        await provider._fetch_whale_taker_ratio()

        provider._perp.fapiPublicGetKlines.assert_awaited_once()
        provider._perp.fetch_ohlcv.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_klines_falls_back_to_neutral(self):
        provider = self._make_provider()
        provider._perp.fapiPublicGetKlines = AsyncMock(return_value=[])
        ratio = await provider._fetch_whale_taker_ratio()
        assert ratio == 1.0

    @pytest.mark.asyncio
    async def test_ratio_capped_at_ten(self):
        """All-taker-buy volume (no sell side) should cap at 10.0, not inf."""
        provider = self._make_provider()
        provider._perp.fapiPublicGetKlines = AsyncMock(
            return_value=[
                [0, "1", "1", "1", "1", "1000", 0, "1000", "1", "1000", "1000", "0"],
            ]
        )
        ratio = await provider._fetch_whale_taker_ratio()
        assert ratio == 10.0


# ---------------------------------------------------------------------------
# Bug 2: fabricated-confidence fix in IntelligenceAnalyzer.compute_metrics
# ---------------------------------------------------------------------------


class TestComputeMetricsConfidenceFix:
    def test_unimplemented_fields_are_nan_not_fabricated_constants(self):
        analyzer = IntelligenceAnalyzer()
        metrics = analyzer.compute_metrics(
            exchange_netflow={"netflow": 100.0},
            whale_activity={"ratio": 1.5},
            funding_rate={"rate_pct": 0.02},
        )

        # These 11 fields have no real free data source wired in yet and
        # must be NaN, not the old hardcoded 0.35/0.25/0.0 placeholders.
        unimplemented = [
            metrics.exchange_reserve_ratio,
            metrics.miner_netflow_signal,
            metrics.staking_unlock_risk,
            metrics.entity_exchange_imbalance,
            metrics.liquidation_pressure_24h_zscore,
            metrics.futures_oi_change_pct,
            metrics.liquidation_cascade_risk_usd,
            metrics.btc_dominance_regime,
            metrics.stablecoin_reserve_ratio,
            metrics.network_activity_score,
            metrics.cross_exchange_basis_spread_bps,
        ]
        for value in unimplemented:
            assert math.isnan(value)

    def test_confidence_reflects_fraction_of_real_metrics_not_near_one(self):
        analyzer = IntelligenceAnalyzer()
        metrics = analyzer.compute_metrics(
            exchange_netflow={"netflow": 100.0},
            whale_activity={"ratio": 1.5},
            funding_rate={"rate_pct": 0.02},
        )
        # Old bug: confidence could read ~1.0 with 14/18 fields fabricated.
        # Fixed: confidence must be capped near the real fraction (4/18).
        assert metrics.confidence <= (4 / 18) + 1e-9
        assert metrics.confidence > 0.0  # the 4 real/derived fields still count

    def test_real_fields_are_still_populated_normally(self):
        analyzer = IntelligenceAnalyzer()
        metrics = analyzer.compute_metrics(
            exchange_netflow={"netflow": 100.0},
            whale_activity={"ratio": 2.0},
            funding_rate={"rate_pct": 0.02},
        )
        assert not math.isnan(metrics.exchange_netflow_7d_zscore)
        assert not math.isnan(metrics.whale_buy_sell_ratio)
        assert not math.isnan(metrics.binance_funding_rate_pct)
        assert not math.isnan(metrics.exchange_stress_score)
        assert metrics.binance_funding_rate_pct == pytest.approx(0.02)

    def test_whale_buy_sell_ratio_is_normalized_not_raw(self):
        """UI-010: whale_buy_sell_ratio must be the normalized [-1, 1] value
        (the log/np.clip computation was previously computed and discarded,
        silently returning the raw unbounded ratio instead)."""
        analyzer = IntelligenceAnalyzer()
        # A large raw ratio (50) must clamp to +1.0, not propagate as 50.
        metrics = analyzer.compute_metrics(
            exchange_netflow={"netflow": 100.0},
            whale_activity={"ratio": 50.0},
            funding_rate={"rate_pct": 0.02},
        )
        assert metrics.whale_buy_sell_ratio == pytest.approx(1.0)

        # A tiny raw ratio (near 0, heavy selling) must clamp to -1.0.
        metrics_bearish = analyzer.compute_metrics(
            exchange_netflow={"netflow": 100.0},
            whale_activity={"ratio": 0.001},
            funding_rate={"rate_pct": 0.02},
        )
        assert metrics_bearish.whale_buy_sell_ratio == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# _compute_zscore and _compute_exchange_stress (pure private methods)
# ---------------------------------------------------------------------------


class TestComputeZscore:
    def _analyzer_with_history(self, values: list[float], col: str) -> IntelligenceAnalyzer:
        import pandas as pd

        a = IntelligenceAnalyzer()
        a.historical_data = pd.DataFrame({col: values})
        return a

    def test_empty_history_returns_zero(self) -> None:
        import pandas as pd

        a = IntelligenceAnalyzer()
        a.historical_data = pd.DataFrame()
        assert a._compute_zscore(5.0, "some_metric") == 0.0

    def test_missing_column_returns_zero(self) -> None:
        import pandas as pd

        a = IntelligenceAnalyzer()
        a.historical_data = pd.DataFrame({"other_col": [1.0, 2.0, 3.0]})
        assert a._compute_zscore(5.0, "missing") == 0.0

    def test_single_row_returns_zero(self) -> None:
        a = self._analyzer_with_history([5.0], "col")
        assert a._compute_zscore(5.0, "col") == 0.0

    def test_constant_series_returns_zero(self) -> None:
        a = self._analyzer_with_history([3.0, 3.0, 3.0], "col")
        assert a._compute_zscore(3.0, "col") == 0.0

    def test_positive_zscore_for_high_value(self) -> None:
        a = self._analyzer_with_history([1.0, 2.0, 3.0, 4.0, 5.0], "col")
        result = a._compute_zscore(10.0, "col")
        assert result > 0.0

    def test_negative_zscore_for_low_value(self) -> None:
        a = self._analyzer_with_history([1.0, 2.0, 3.0, 4.0, 5.0], "col")
        result = a._compute_zscore(-10.0, "col")
        assert result < 0.0

    def test_clamped_to_minus5_plus5(self) -> None:
        a = self._analyzer_with_history([0.0, 0.0, 0.0, 0.0, 1.0], "col")
        result_high = a._compute_zscore(1e9, "col")
        result_low = a._compute_zscore(-1e9, "col")
        assert result_high == pytest.approx(5.0)
        assert result_low == pytest.approx(-5.0)


class TestComputeExchangeStress:
    def _analyzer(self) -> IntelligenceAnalyzer:
        return IntelligenceAnalyzer()

    def test_normal_conditions_near_zero(self) -> None:
        a = self._analyzer()
        result = a._compute_exchange_stress(netflow_zscore=0.5, funding_signal=0.1)
        assert result == pytest.approx(0.0)

    def test_high_negative_netflow_adds_stress(self) -> None:
        a = self._analyzer()
        result = a._compute_exchange_stress(netflow_zscore=-3.0, funding_signal=0.0)
        assert result > 0.0

    def test_high_funding_adds_stress(self) -> None:
        a = self._analyzer()
        result = a._compute_exchange_stress(netflow_zscore=0.0, funding_signal=0.8)
        assert result == pytest.approx(0.3)

    def test_combined_stress_bounded_at_one(self) -> None:
        a = self._analyzer()
        result = a._compute_exchange_stress(netflow_zscore=-100.0, funding_signal=1.0)
        assert 0.0 <= result <= 1.0

    def test_netflow_stress_capped_at_half(self) -> None:
        a = self._analyzer()
        result_extreme = a._compute_exchange_stress(netflow_zscore=-1000.0, funding_signal=0.0)
        assert result_extreme <= 0.5
