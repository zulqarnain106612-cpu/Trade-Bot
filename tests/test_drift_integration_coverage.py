"""
Coverage for src/risk/drift_integration.py — Debt-005.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.risk.drift_integration import DriftIntegrationAdapter
from src.risk.performance_drift import DriftDetected, PerformanceDriftDetector


def _detector() -> MagicMock:
    d = MagicMock(spec=PerformanceDriftDetector)
    d.record_trade_outcome = MagicMock()
    d.check_drift = MagicMock(return_value=DriftDetected(drifted=False, reason='ok'))
    d.get_live_metrics = MagicMock(return_value={})
    return d


class TestDriftIntegrationAdapterInit:
    def test_stores_detector(self):
        det = _detector()
        adapter = DriftIntegrationAdapter(drift_detector=det)
        assert adapter._detector is det

    def test_accepts_none_detector(self):
        adapter = DriftIntegrationAdapter(drift_detector=None)
        assert adapter._detector is None


class TestRecordClosedTrade:
    @pytest.mark.asyncio
    async def test_none_detector_is_noop(self):
        adapter = DriftIntegrationAdapter(drift_detector=None)
        # Should return without error
        await adapter.record_closed_trade(
            trade_id="T1", exit_price=100.0, pnl_usd=50.0,
            predicted_prob=0.7, actual_direction=1,
            current_equity=10_050.0, starting_equity=10_000.0,
        )

    @pytest.mark.asyncio
    async def test_calls_detector_record_trade(self):
        det = _detector()
        adapter = DriftIntegrationAdapter(drift_detector=det)
        await adapter.record_closed_trade(
            trade_id="T1", exit_price=105.0, pnl_usd=50.0,
            predicted_prob=0.7, actual_direction=1,
            current_equity=10_050.0, starting_equity=10_000.0,
        )
        det.record_trade_outcome.assert_called_once()

    @pytest.mark.asyncio
    async def test_loss_trade_recorded(self):
        det = _detector()
        adapter = DriftIntegrationAdapter(drift_detector=det)
        await adapter.record_closed_trade(
            trade_id="T2", exit_price=95.0, pnl_usd=-100.0,
            predicted_prob=0.8, actual_direction=-1,
            current_equity=9_900.0, starting_equity=10_000.0,
        )
        det.record_trade_outcome.assert_called_once()

    @pytest.mark.asyncio
    async def test_detector_exception_does_not_propagate(self):
        """Drift recording errors must not crash the orchestrator."""
        det = _detector()
        det.record_trade_outcome.side_effect = RuntimeError("drift DB down")
        adapter = DriftIntegrationAdapter(drift_detector=det)
        # Should swallow the error gracefully
        try:
            await adapter.record_closed_trade(
                trade_id="T3", exit_price=100.0, pnl_usd=10.0,
                predicted_prob=0.6, actual_direction=1,
                current_equity=10_010.0, starting_equity=10_000.0,
            )
        except RuntimeError:
            pytest.fail("record_closed_trade should not propagate drift errors")


class TestCheckDrift:
    def test_no_detector_returns_not_drifted(self):
        adapter = DriftIntegrationAdapter(drift_detector=None)
        result = adapter.check_drift()
        assert result["drifted"] is False
        assert "reason" in result

    def test_detector_not_drifted(self):
        det = _detector()
        adapter = DriftIntegrationAdapter(drift_detector=det)
        result = adapter.check_drift()
        assert isinstance(result, dict)
        assert "drifted" in result

    def test_detector_drifted(self):
        det = _detector()
        det.check_drift.return_value = DriftDetected(drifted=True, metric='sharpe', reason='low sharpe')
        adapter = DriftIntegrationAdapter(drift_detector=det)
        result = adapter.check_drift()
        assert result["drifted"] is True
