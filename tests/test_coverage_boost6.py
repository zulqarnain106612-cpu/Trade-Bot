"""Coverage boost 6: DrawdownTracker edge cases, exchange stress medium path,
performance drift, coingecko zscore path, blockchain n_txs=0 branch,
HTTP fetch paths for blockchain/coingecko providers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.intelligence.providers.blockchain_provider import BlockchainIntelligenceProvider
from src.intelligence.providers.coingecko_provider import CoinGeckoIntelligenceProvider
from src.risk.gates import (
    DrawdownTracker,
    GateStatus,
    check_exchange_stress,
    check_performance_drift,
)
from src.risk.performance_drift import DriftDetected


# ---------------------------------------------------------------------------
# DrawdownTracker edge-case properties (lines 621-654)
# ---------------------------------------------------------------------------


class TestDrawdownTrackerEdgeCases:
    def test_daily_pnl_pct_zero_daily_start(self):
        # force _daily_start = 0 via reset_daily to hit guard branch
        tracker = DrawdownTracker(starting_equity=100_000.0)
        tracker.reset_daily(0.0)
        assert tracker.daily_pnl_pct == 0.0

    def test_daily_pnl_pct_normal(self):
        tracker = DrawdownTracker(starting_equity=100_000.0)
        tracker.update(105_000.0)
        assert tracker.daily_pnl_pct == pytest.approx(5.0)

    def test_drawdown_from_daily_start_pct_zero_daily_start(self):
        tracker = DrawdownTracker(starting_equity=100_000.0)
        tracker.reset_daily(0.0)
        assert tracker.drawdown_from_daily_start_pct == 0.0

    def test_drawdown_from_daily_start_pct_normal(self):
        tracker = DrawdownTracker(starting_equity=100_000.0)
        tracker.update(90_000.0)
        assert tracker.drawdown_from_daily_start_pct == pytest.approx(-10.0)

    def test_reset_daily_updates_daily_start(self):
        tracker = DrawdownTracker(starting_equity=100_000.0)
        tracker.update(110_000.0)
        tracker.reset_daily(110_000.0)
        assert tracker.daily_pnl_usd == pytest.approx(0.0)

    def test_starting_equity_property(self):
        tracker = DrawdownTracker(starting_equity=50_000.0)
        assert tracker.starting_equity == pytest.approx(50_000.0)

    def test_daily_start_equity_property(self):
        tracker = DrawdownTracker(starting_equity=80_000.0)
        assert tracker.daily_start_equity == pytest.approx(80_000.0)


# ---------------------------------------------------------------------------
# check_exchange_stress — medium path (0.50-0.75 → log warning, still PASS)
# ---------------------------------------------------------------------------


class TestExchangeStressMediumPath:
    def test_medium_stress_passes_with_warning(self):
        # 0.60 > 0.50 (reduce threshold) but <= 0.75 (halt threshold)
        r = check_exchange_stress(0.60)
        assert r.passed
        assert r.details.get("stress_action") == "reduce_suggested"

    def test_borderline_halt_threshold_fails(self):
        r = check_exchange_stress(0.80)
        assert not r.passed

    def test_exactly_at_halt_threshold_fails(self):
        # > 0.75 → halt
        r = check_exchange_stress(0.76)
        assert not r.passed


# ---------------------------------------------------------------------------
# check_performance_drift (lines 703, 707)
# ---------------------------------------------------------------------------


class TestCheckPerformanceDrift:
    def test_none_detector_passes(self):
        r = check_performance_drift(None)
        assert r.passed

    def test_drifted_detector_fails(self):
        drift = DriftDetected(
            drifted=True,
            reason="sharpe degraded",
            metric="sharpe",
            live_value=0.1,
            baseline_value=2.0,
            drift_pp=-1.9,
        )
        detector = MagicMock()
        detector.check_drift.return_value = drift
        r = check_performance_drift(detector)
        assert not r.passed
        assert r.status == GateStatus.HALT_DRIFT

    def test_passing_detector_passes(self):
        drift = DriftDetected(
            drifted=False,
            reason="all good",
            metric="",
            live_value=0.0,
            baseline_value=0.0,
            drift_pp=0.0,
        )
        detector = MagicMock()
        detector.check_drift.return_value = drift
        r = check_performance_drift(detector)
        assert r.passed


# ---------------------------------------------------------------------------
# CoinGeckoIntelligenceProvider — btc dominance z-score path (line 98)
# ---------------------------------------------------------------------------


class TestCoinGeckoZscorePath:
    @pytest.mark.asyncio
    async def test_fetch_metrics_btc_dom_zscore_computed(self):
        p = CoinGeckoIntelligenceProvider()
        # Pre-populate history so len >= 4 → zscore branch executes
        p._btc_dom_history = [50.0, 51.0, 52.0, 53.0]
        global_data = {
            "market_cap_percentage": {"btc": 55.0, "usdt": 5.0, "usdc": 3.0},
            "total_market_cap": {"usd": 2_000_000_000_000.0},
        }
        with patch.object(p, "_fetch_global", new=AsyncMock(return_value=global_data)):
            result = await p.fetch_metrics()
        assert "btc_dominance_regime" in result
        # zscore should be non-zero since history shows variance
        assert isinstance(result["btc_dominance_regime"], float)

    @pytest.mark.asyncio
    async def test_fetch_global_network_call(self):
        """Cover _fetch_global HTTP path (lines 157-167)."""
        p = CoinGeckoIntelligenceProvider()
        body = {"data": {"market_cap_percentage": {"btc": 50.0}}}
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=body)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await p._fetch_global()
        assert result.get("market_cap_percentage", {}).get("btc") == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# BlockchainIntelligenceProvider — n_txs=0 branch + HTTP fetch path
# ---------------------------------------------------------------------------


class TestBlockchainEdgeCases:
    @pytest.mark.asyncio
    async def test_fetch_metrics_ntxs_zero_not_appended(self):
        """hash_rate > 0 but n_txs == 0 → _tx_history stays empty (branch 99->103)."""
        p = BlockchainIntelligenceProvider()
        stats = {"hash_rate": 500_000.0, "n_tx": 0.0}
        with patch.object(p, "_fetch_stats", new=AsyncMock(return_value=stats)):
            await p.fetch_metrics()
        assert len(p._tx_history) == 0
        assert len(p._hashrate_history) == 1  # hash_rate was appended

    @pytest.mark.asyncio
    async def test_fetch_stats_network_call(self):
        """Cover _fetch_stats HTTP path (lines 151-160)."""
        p = BlockchainIntelligenceProvider()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"hash_rate": 600_000.0, "n_tx": 350_000.0})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await p._fetch_stats()
        assert result["hash_rate"] == pytest.approx(600_000.0)
