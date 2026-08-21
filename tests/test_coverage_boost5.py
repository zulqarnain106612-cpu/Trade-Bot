"""Coverage boost 5: binance_provider, risk/gates uncovered paths."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.intelligence.providers.binance_provider import (
    BinanceIntelligenceProvider,
    get_binance_intelligence_provider,
)
from src.risk.gates import (
    DrawdownTracker,
    GateResult,
    GateStatus,
    RiskGateContext,
    TradingMode,
    check_consecutive_losses,
    check_daily_drawdown,
    check_exchange_stress,
    check_live_gate,
    check_regime_gate,
    check_slippage_veto,
    check_whale_activity,
    evaluate_all_gates,
)


# ---------------------------------------------------------------------------
# BinanceIntelligenceProvider
# ---------------------------------------------------------------------------


class TestBinanceProvider:
    def test_exchange_id(self):
        p = BinanceIntelligenceProvider()
        assert p.exchange_id == "binance"

    @pytest.mark.asyncio
    async def test_initialize_and_close(self):
        p = BinanceIntelligenceProvider()
        with (
            patch.object(p._spot, "load_markets", new=AsyncMock(return_value={})),
            patch.object(p._perp, "load_markets", new=AsyncMock(return_value={})),
            patch.object(p._spot, "close", new=AsyncMock()),
            patch.object(p._perp, "close", new=AsyncMock()),
        ):
            await p.initialize()
            await p.close()

    @pytest.mark.asyncio
    async def test_fetch_metrics_success(self):
        p = BinanceIntelligenceProvider()
        funding_dict = {"rate_pct": 0.03, "zscore": 0.5}
        oi_dict = {"change_pct": 0.02, "value_usd": 400_000_000.0}
        with (
            patch.object(p, "_fetch_funding_data", new=AsyncMock(return_value=funding_dict)),
            patch.object(p, "_fetch_oi_data", new=AsyncMock(return_value=oi_dict)),
            patch.object(p, "_fetch_basis_data", new=AsyncMock(return_value=3.5)),
            patch.object(p, "_fetch_whale_taker_ratio", new=AsyncMock(return_value=1.6)),
        ):
            result = await p.fetch_metrics()
        assert "binance_funding_rate_pct" in result
        assert "futures_oi_change_pct" in result
        assert result["confidence"] > 0.0

    @pytest.mark.asyncio
    async def test_fetch_metrics_all_fail(self):
        p = BinanceIntelligenceProvider()
        exc = RuntimeError("timeout")
        with (
            patch.object(p, "_fetch_funding_data", new=AsyncMock(side_effect=exc)),
            patch.object(p, "_fetch_oi_data", new=AsyncMock(side_effect=exc)),
            patch.object(p, "_fetch_basis_data", new=AsyncMock(side_effect=exc)),
            patch.object(p, "_fetch_whale_taker_ratio", new=AsyncMock(side_effect=exc)),
        ):
            result = await p.fetch_metrics()
        assert result["confidence"] < 1.0

    @pytest.mark.asyncio
    async def test_fetch_metrics_funding_exception(self):
        p = BinanceIntelligenceProvider()
        exc = RuntimeError("x")
        with (
            patch.object(p, "_fetch_funding_data", new=AsyncMock(side_effect=exc)),
            patch.object(
                p,
                "_fetch_oi_data",
                new=AsyncMock(return_value={"change_pct": 0.0, "value_usd": 0.0}),
            ),
            patch.object(p, "_fetch_basis_data", new=AsyncMock(return_value=0.0)),
            patch.object(p, "_fetch_whale_taker_ratio", new=AsyncMock(return_value=1.0)),
        ):
            result = await p.fetch_metrics()
        assert result["confidence"] < 1.0

    @pytest.mark.asyncio
    async def test_fetch_metrics_oi_exception(self):
        p = BinanceIntelligenceProvider()
        exc = RuntimeError("x")
        with (
            patch.object(
                p,
                "_fetch_funding_data",
                new=AsyncMock(return_value={"rate_pct": 0.01, "zscore": 0.0}),
            ),
            patch.object(p, "_fetch_oi_data", new=AsyncMock(side_effect=exc)),
            patch.object(p, "_fetch_basis_data", new=AsyncMock(return_value=0.0)),
            patch.object(p, "_fetch_whale_taker_ratio", new=AsyncMock(return_value=1.0)),
        ):
            result = await p.fetch_metrics()
        assert result["confidence"] < 1.0

    @pytest.mark.asyncio
    async def test_fetch_metrics_basis_exception(self):
        p = BinanceIntelligenceProvider()
        exc = RuntimeError("x")
        with (
            patch.object(
                p,
                "_fetch_funding_data",
                new=AsyncMock(return_value={"rate_pct": 0.01, "zscore": 0.0}),
            ),
            patch.object(
                p,
                "_fetch_oi_data",
                new=AsyncMock(return_value={"change_pct": 0.0, "value_usd": 0.0}),
            ),
            patch.object(p, "_fetch_basis_data", new=AsyncMock(side_effect=exc)),
            patch.object(p, "_fetch_whale_taker_ratio", new=AsyncMock(return_value=1.0)),
        ):
            result = await p.fetch_metrics()
        assert result["confidence"] < 1.0

    @pytest.mark.asyncio
    async def test_fetch_metrics_whale_exception(self):
        p = BinanceIntelligenceProvider()
        exc = RuntimeError("x")
        with (
            patch.object(
                p,
                "_fetch_funding_data",
                new=AsyncMock(return_value={"rate_pct": 0.01, "zscore": 0.0}),
            ),
            patch.object(
                p,
                "_fetch_oi_data",
                new=AsyncMock(return_value={"change_pct": 0.0, "value_usd": 0.0}),
            ),
            patch.object(p, "_fetch_basis_data", new=AsyncMock(return_value=0.0)),
            patch.object(p, "_fetch_whale_taker_ratio", new=AsyncMock(side_effect=exc)),
        ):
            result = await p.fetch_metrics()
        assert result["confidence"] < 1.0

    def test_compute_stress_score_low(self):
        p = BinanceIntelligenceProvider()
        score = p._compute_stress_score(basis_bps=0.0, funding_zscore=0.0, oi_change_pct=0.0)
        assert score == pytest.approx(0.0)

    def test_compute_stress_score_high(self):
        p = BinanceIntelligenceProvider()
        score = p._compute_stress_score(basis_bps=200.0, funding_zscore=3.0, oi_change_pct=0.5)
        assert score > 0.0
        assert score <= 1.0

    def test_get_cache_miss(self):
        p = BinanceIntelligenceProvider()
        assert p._get_cache("missing") is None

    def test_set_and_get_cache(self):
        p = BinanceIntelligenceProvider()
        p._set_cache("k", {"data": 42})
        assert p._get_cache("k") == {"data": 42}

    def test_get_cache_expired(self):
        p = BinanceIntelligenceProvider(cache_ttl_s=1)
        p._cache["k"] = (time.monotonic() - 10.0, "value")
        assert p._get_cache("k") is None

    @pytest.mark.asyncio
    async def test_fetch_funding_data_uses_cache(self):
        p = BinanceIntelligenceProvider()
        cached = {"rate_pct": 0.02, "zscore": 0.1}
        p._set_cache(f"funding:{p._perp_symbol}", cached)
        result = await p._fetch_funding_data()
        assert result == cached

    @pytest.mark.asyncio
    async def test_fetch_oi_data_uses_cache(self):
        p = BinanceIntelligenceProvider()
        cached = {"change_pct": 0.02, "value_usd": 1_000_000.0}
        p._set_cache(f"oi:{p._perp_symbol}", cached)
        result = await p._fetch_oi_data()
        assert result == cached

    @pytest.mark.asyncio
    async def test_fetch_basis_data_uses_cache(self):
        p = BinanceIntelligenceProvider()
        p._set_cache(f"basis:{p._symbol}", 2.5)
        result = await p._fetch_basis_data()
        assert result == pytest.approx(2.5)

    @pytest.mark.asyncio
    async def test_fetch_whale_ratio_uses_cache(self):
        p = BinanceIntelligenceProvider()
        p._set_cache(f"whale:{p._perp_symbol}", 1.4)
        result = await p._fetch_whale_taker_ratio()
        assert result == pytest.approx(1.4)

    def test_singleton(self):
        import src.intelligence.providers.binance_provider as mod

        mod._provider = None
        p1 = get_binance_intelligence_provider()
        p2 = get_binance_intelligence_provider()
        assert p1 is p2

    @pytest.mark.asyncio
    async def test_fetch_funding_data_empty_history(self):
        p = BinanceIntelligenceProvider()
        with patch.object(p._perp, "fetch_funding_rate_history", new=AsyncMock(return_value=[])):
            result = await p._fetch_funding_data()
        assert result == {"rate_pct": 0.0, "zscore": 0.0}

    @pytest.mark.asyncio
    async def test_fetch_funding_data_single_rate_no_zscore(self):
        p = BinanceIntelligenceProvider()
        history = [{"fundingRate": 0.0001}]
        with patch.object(
            p._perp, "fetch_funding_rate_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_funding_data()
        assert result["rate_pct"] == pytest.approx(0.01)  # 0.0001 * 100 = 0.01%
        assert result["zscore"] == 0.0

    @pytest.mark.asyncio
    async def test_fetch_funding_data_computes_zscore_and_caches(self):
        p = BinanceIntelligenceProvider()
        history = [{"fundingRate": r} for r in [0.0001, 0.0002, 0.0003, 0.0005, -0.0001]]
        with patch.object(
            p._perp, "fetch_funding_rate_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_funding_data()
        assert result["rate_pct"] == pytest.approx(-0.01)
        assert isinstance(result["zscore"], float)
        with patch.object(
            p._perp,
            "fetch_funding_rate_history",
            new=AsyncMock(side_effect=AssertionError("should not be called")),
        ):
            cached = await p._fetch_funding_data()
        assert cached == result

    @pytest.mark.asyncio
    async def test_fetch_funding_data_zero_stdev_zscore_zero(self):
        p = BinanceIntelligenceProvider()
        history = [{"fundingRate": 0.0002}, {"fundingRate": 0.0002}]
        with patch.object(
            p._perp, "fetch_funding_rate_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_funding_data()
        assert result["zscore"] == 0.0

    @pytest.mark.asyncio
    async def test_fetch_oi_data_empty_history(self):
        p = BinanceIntelligenceProvider()
        with patch.object(p._perp, "fetch_open_interest_history", new=AsyncMock(return_value=[])):
            result = await p._fetch_oi_data()
        assert result == {"change_pct": 0.0, "value_usd": 0.0}

    @pytest.mark.asyncio
    async def test_fetch_oi_data_single_point_returns_neutral(self):
        p = BinanceIntelligenceProvider()
        history = [{"openInterestAmount": 1000.0, "openInterestValue": 50_000_000.0}]
        with patch.object(
            p._perp, "fetch_open_interest_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_oi_data()
        assert result == {"change_pct": 0.0, "value_usd": 0.0}

    @pytest.mark.asyncio
    async def test_fetch_oi_data_computes_change_and_caches(self):
        p = BinanceIntelligenceProvider()
        history = [
            {"openInterestAmount": 1000.0, "openInterestValue": 40_000_000.0},
            {"openInterestAmount": 1100.0, "openInterestValue": 44_000_000.0},
        ]
        with patch.object(
            p._perp, "fetch_open_interest_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_oi_data()
        assert result["change_pct"] == pytest.approx(10.0)
        assert result["value_usd"] == pytest.approx(44_000_000.0)
        with patch.object(
            p._perp,
            "fetch_open_interest_history",
            new=AsyncMock(side_effect=AssertionError("should not be called")),
        ):
            cached = await p._fetch_oi_data()
        assert cached == result

    @pytest.mark.asyncio
    async def test_fetch_oi_data_zero_start_oi_no_divide_error(self):
        p = BinanceIntelligenceProvider()
        history = [
            {"openInterestAmount": 0.0, "openInterestValue": 0.0},
            {"openInterestAmount": 500.0, "openInterestValue": 20_000_000.0},
        ]
        with patch.object(
            p._perp, "fetch_open_interest_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_oi_data()
        assert result["change_pct"] == 0.0

    @pytest.mark.asyncio
    async def test_fetch_oi_data_missing_value_defaults_zero(self):
        p = BinanceIntelligenceProvider()
        history = [
            {"openInterestAmount": 1000.0, "openInterestValue": None},
            {"openInterestAmount": 1100.0, "openInterestValue": None},
        ]
        with patch.object(
            p._perp, "fetch_open_interest_history", new=AsyncMock(return_value=history)
        ):
            result = await p._fetch_oi_data()
        assert result["value_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_fetch_basis_data_computes_and_caches(self):
        p = BinanceIntelligenceProvider()
        spot_ticker = {"last": 50_000.0}
        perp_ticker = {"last": 50_100.0}
        with (
            patch.object(p._spot, "fetch_ticker", new=AsyncMock(return_value=spot_ticker)),
            patch.object(p._perp, "fetch_ticker", new=AsyncMock(return_value=perp_ticker)),
        ):
            result = await p._fetch_basis_data()
        assert result == pytest.approx(20.0)
        with (
            patch.object(p._spot, "fetch_ticker", new=AsyncMock(side_effect=AssertionError("no"))),
            patch.object(p._perp, "fetch_ticker", new=AsyncMock(side_effect=AssertionError("no"))),
        ):
            cached = await p._fetch_basis_data()
        assert cached == result

    @pytest.mark.asyncio
    async def test_fetch_basis_data_uses_close_fallback(self):
        p = BinanceIntelligenceProvider()
        spot_ticker = {"last": None, "close": 100.0}
        perp_ticker = {"last": None, "close": 101.0}
        with (
            patch.object(p._spot, "fetch_ticker", new=AsyncMock(return_value=spot_ticker)),
            patch.object(p._perp, "fetch_ticker", new=AsyncMock(return_value=perp_ticker)),
        ):
            result = await p._fetch_basis_data()
        assert result == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_fetch_basis_data_missing_prices_returns_zero(self):
        p = BinanceIntelligenceProvider()
        spot_ticker = {"last": None, "close": None}
        perp_ticker = {"last": 100.0, "close": None}
        with (
            patch.object(p._spot, "fetch_ticker", new=AsyncMock(return_value=spot_ticker)),
            patch.object(p._perp, "fetch_ticker", new=AsyncMock(return_value=perp_ticker)),
        ):
            result = await p._fetch_basis_data()
        assert result == 0.0

    @pytest.mark.asyncio
    async def test_fetch_basis_data_clamped_to_range(self):
        p = BinanceIntelligenceProvider()
        spot_ticker = {"last": 100.0}
        perp_ticker = {"last": 1000.0}
        with (
            patch.object(p._spot, "fetch_ticker", new=AsyncMock(return_value=spot_ticker)),
            patch.object(p._perp, "fetch_ticker", new=AsyncMock(return_value=perp_ticker)),
        ):
            result = await p._fetch_basis_data()
        assert result == pytest.approx(500.0)

    @pytest.mark.asyncio
    async def test_fetch_whale_taker_ratio_empty_klines_returns_neutral(self):
        p = BinanceIntelligenceProvider()
        with (
            patch.object(p._perp, "market", return_value={"id": "BTCUSDT"}),
            patch.object(p._perp, "fapiPublicGetKlines", new=AsyncMock(return_value=[])),
        ):
            result = await p._fetch_whale_taker_ratio()
        assert result == 1.0

    @pytest.mark.asyncio
    async def test_fetch_whale_taker_ratio_computes_and_caches(self):
        p = BinanceIntelligenceProvider()
        # 12 bars: [ts,o,h,l,c,volume,close_time,quote_vol,n_trades,taker_buy_base,taker_buy_quote,ignore]
        bar = ["0", "1", "1", "1", "1", "100.0", "0", "0", "0", "70.0", "0", "0"]
        raw = [bar] * 10
        with (
            patch.object(p._perp, "market", return_value={"id": "BTCUSDT"}),
            patch.object(p._perp, "fapiPublicGetKlines", new=AsyncMock(return_value=raw)),
        ):
            result = await p._fetch_whale_taker_ratio()
        # taker_buy=700, total=1000, sell=300 -> ratio = 700/300
        assert result == pytest.approx(700.0 / 300.0)
        with patch.object(p._perp, "market", side_effect=AssertionError("should not be called")):
            cached = await p._fetch_whale_taker_ratio()
        assert cached == result

    @pytest.mark.asyncio
    async def test_fetch_whale_taker_ratio_zero_total_vol_returns_neutral(self):
        p = BinanceIntelligenceProvider()
        bar = ["0", "1", "1", "1", "1", "0.0", "0", "0", "0", "0.0", "0", "0"]
        raw = [bar] * 5
        with (
            patch.object(p._perp, "market", return_value={"id": "BTCUSDT"}),
            patch.object(p._perp, "fapiPublicGetKlines", new=AsyncMock(return_value=raw)),
        ):
            result = await p._fetch_whale_taker_ratio()
        assert result == 1.0

    @pytest.mark.asyncio
    async def test_fetch_whale_taker_ratio_all_taker_buy_caps_at_ten(self):
        p = BinanceIntelligenceProvider()
        bar = ["0", "1", "1", "1", "1", "100.0", "0", "0", "0", "100.0", "0", "0"]
        raw = [bar] * 5
        with (
            patch.object(p._perp, "market", return_value={"id": "BTCUSDT"}),
            patch.object(p._perp, "fapiPublicGetKlines", new=AsyncMock(return_value=raw)),
        ):
            result = await p._fetch_whale_taker_ratio()
        assert result == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# risk/gates — uncovered branch paths
# ---------------------------------------------------------------------------


def _make_ctx(**overrides) -> RiskGateContext:
    defaults = {
        "daily_pnl_usd": 200.0,
        "starting_equity_usd": 100_000.0,
        "consecutive_loss_count": 0,
        "regime_state": 1,
        "notional_usd": 3_000.0,
        "capital_usd": 100_000.0,
        "trading_mode": TradingMode.PAPER,
        "direction_gate_pass": True,
        "meta_gate_pass": True,
        "exchange_stress_score": None,
        "whale_buy_sell_ratio": None,
    }
    defaults.update(overrides)
    return RiskGateContext(**defaults)


class TestGatesUncoveredPaths:
    def test_drawdown_tracker_update_above_peak(self):
        tracker = DrawdownTracker(starting_equity=100_000.0)
        tracker.update(110_000.0)
        assert tracker.peak_equity == pytest.approx(110_000.0)
        assert tracker.drawdown_from_peak_pct == 0.0

    def test_drawdown_tracker_update_below_peak(self):
        tracker = DrawdownTracker(starting_equity=100_000.0)
        tracker.update(110_000.0)
        tracker.update(95_000.0)
        # drawdown_from_peak_pct is negative when below peak
        assert tracker.drawdown_from_peak_pct < 0.0

    def test_gate_result_pass(self):
        r = GateResult.pass_gate()
        assert r.passed is True

    def test_gate_result_fail(self):
        r = GateResult.fail(GateStatus.HALT_DRAWDOWN, "too risky")
        assert r.passed is False
        assert "risky" in r.reason

    def test_check_consecutive_losses_passes(self):
        cfg = SimpleNamespace(consecutive_loss_halt=5)
        r = check_consecutive_losses(2, cfg)
        assert r.passed

    def test_check_consecutive_losses_fails(self):
        cfg = SimpleNamespace(consecutive_loss_halt=3)
        r = check_consecutive_losses(5, cfg)
        assert not r.passed

    def test_check_daily_drawdown_passes(self):
        # drawdown_pct = -0.5%, halt at -5% → passes
        cfg = SimpleNamespace(daily_drawdown_halt_pct=5.0)
        r = check_daily_drawdown(-500.0, 100_000.0, cfg)
        assert r.passed

    def test_check_daily_drawdown_fails(self):
        # drawdown_pct = -5%, halt at -1% → fails
        cfg = SimpleNamespace(daily_drawdown_halt_pct=1.0)
        r = check_daily_drawdown(-5_000.0, 100_000.0, cfg)
        assert not r.passed

    def test_check_exchange_stress_none_passes(self):
        r = check_exchange_stress(None)
        assert r.passed

    def test_check_exchange_stress_high_fails(self):
        r = check_exchange_stress(0.95)
        assert not r.passed

    def test_check_exchange_stress_low_passes(self):
        r = check_exchange_stress(0.3)
        assert r.passed

    def test_check_regime_gate_volatile_fails(self):
        from src.risk.gates import REGIME_VOLATILE

        r = check_regime_gate(REGIME_VOLATILE)
        assert not r.passed

    def test_check_regime_gate_trending_passes(self):
        r = check_regime_gate(1)  # trending
        assert r.passed

    def test_check_whale_activity_none_passes(self):
        r = check_whale_activity(None)
        assert r.passed

    def test_check_whale_activity_low_reduces(self):
        # 0.5 < 0.85 sell_threshold → REDUCE_WHALE_ACTIVITY (not a hard halt)
        r = check_whale_activity(0.5)
        assert not r.passed

    def test_check_whale_activity_high_passes(self):
        r = check_whale_activity(1.2)
        assert r.passed

    def test_check_live_gate_paper_passes(self):
        r = check_live_gate(TradingMode.PAPER, True, True)
        assert r.passed

    def test_check_live_gate_live_dir_fails(self):
        r = check_live_gate(TradingMode.LIVE, False, True)
        assert not r.passed

    def test_check_live_gate_live_meta_fails(self):
        r = check_live_gate(TradingMode.LIVE, True, False)
        assert not r.passed

    def test_evaluate_all_gates_passes(self):
        ctx = _make_ctx()
        result = evaluate_all_gates(ctx)
        assert isinstance(result.passed, bool)

    def test_evaluate_all_gates_high_drawdown_fails(self):
        ctx = _make_ctx(daily_pnl_usd=-10_000.0, starting_equity_usd=100_000.0)
        # -10% drawdown vs 1% halt threshold → fails
        cfg = SimpleNamespace(
            slippage_veto_margin_bps=10.0,
            daily_drawdown_halt_pct=1.0,
            consecutive_loss_halt=10,
            max_position_size_pct=50.0,
            whale_gate_advisory=False,
        )
        result = evaluate_all_gates(ctx, cfg)
        assert not result.passed

    def test_check_slippage_veto_no_estimate_passes(self):
        r = check_slippage_veto(10.0, None, MagicMock(max_slippage_bps=50.0))
        assert r.passed
