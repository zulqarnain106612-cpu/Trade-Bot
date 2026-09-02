"""Residual branches across the filters, drift detector, ensemble predictor,
intelligence client, feature pipeline, router, runtime monitor and ECDSA scan.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Strategy filters: ADX
# ---------------------------------------------------------------------------


def test_adx_reports_zeros_when_the_true_range_has_not_smoothed_yet():
    from src.strategies.filters import adx_dmi

    # fewer bars than the Wilder period -> the true-range EWM is still NaN
    close = pd.Series([100.0, 101.0, 102.0, 101.5])
    adx, plus_di, minus_di = adx_dmi(close + 0.5, close - 0.5, close, period=14)

    assert (adx, plus_di, minus_di) == (0.0, 0.0, 0.0)


def test_a_directionless_market_fails_the_adx_filter():
    from src.config import REGIME_TRENDING
    from src.strategies import filters as mod

    # A clean trend clears every earlier filter, so the ADX refusal is the
    # only thing that can block it -- which is the branch under test.
    close = pd.Series([100.0 + i * 0.5 for i in range(400)])
    with patch.object(mod, "adx_filter_passes", return_value=False):
        result = mod.apply_all_strategy_filters(
            close=close,
            volume=pd.Series([1000.0] * 400),
            atr_series=pd.Series([1.0] * 400),
            direction=1,
            regime_state=REGIME_TRENDING,
            prob_trending=0.8,
            prob_ranging=0.1,
            prob_volatile=0.1,
            high=close + 0.5,
            low=close - 0.5,
        )

    assert result["passes"] is False
    assert "adx_weak_or_misaligned" in result["filters_failed"]


# ---------------------------------------------------------------------------
# Performance drift
# ---------------------------------------------------------------------------


def _detector():
    from src.risk.performance_drift import PerformanceBaseline, PerformanceDriftDetector

    return PerformanceDriftDetector(
        PerformanceBaseline(
            train_sharpe=2.0,
            oos_sharpe=1.5,
            train_accuracy=62.0,
            oos_accuracy=58.0,
            train_win_rate=55.0,
            max_drawdown_pct=8.0,
            trades_in_backtest=400,
        )
    )


def _record(detector, pnl: float, n: int = 1, prob: float = 0.7, direction: int = 1) -> None:
    for _ in range(n):
        detector.record_trade_outcome(
            pnl_usd=pnl,
            predicted_prob=prob,
            actual_direction=direction,
            current_equity=10_000.0,
            starting_equity=10_000.0,
        )


class TestRollingRatios:
    def test_a_short_history_is_not_enough_for_a_rolling_sharpe(self):
        detector = _detector()
        _record(detector, 10.0, n=5)

        assert detector.current_rolling_sharpe() is None

    def test_a_full_window_reports_a_sharpe(self):
        detector = _detector()
        for i in range(25):
            _record(detector, 10.0 if i % 3 else -4.0)

        assert detector.current_rolling_sharpe() is not None

    def test_a_run_with_no_losses_has_no_sortino(self):
        detector = _detector()
        _record(detector, 10.0, n=25)

        # no downside at all -> the ratio is undefined, not infinite
        assert detector.current_rolling_sortino() is None

    def test_a_window_with_losses_reports_a_sortino(self):
        detector = _detector()
        for i in range(25):
            _record(detector, 10.0 if i % 3 else -4.0)

        assert detector.current_rolling_sortino() is not None

    def test_sharpe_drift_needs_a_full_window(self):
        detector = _detector()
        _record(detector, 10.0, n=5)

        assert detector._check_sharpe_drift().drifted is False

    def test_accuracy_drift_needs_at_least_one_prediction(self):
        detector = _detector()

        assert detector._check_accuracy_drift().drifted is False


# ---------------------------------------------------------------------------
# Ensemble predictor
# ---------------------------------------------------------------------------


def test_the_lstm_is_disabled_when_torch_is_not_installed(monkeypatch):
    from src.intelligence import ensemble_predictor as mod

    monkeypatch.setattr(mod, "_TORCH_AVAILABLE", False)
    predictor = mod.LSTMPredictor(lookback=4, epochs=1)

    predictor.fit(np.zeros((8, 4, 1)), np.zeros(8))

    assert predictor.model is None


# ---------------------------------------------------------------------------
# Feature pipeline: bar gap report
# ---------------------------------------------------------------------------


class TestBarGapReport:
    def test_a_single_bar_has_no_measurable_spacing(self):
        from src.features.pipeline import bar_gap_report

        assert bar_gap_report(pd.Index([1_700_000_000_000]))["gap_count"] == 0

    def test_a_non_advancing_index_reports_nothing(self):
        from src.features.pipeline import bar_gap_report

        # duplicate timestamps -> modal spacing of 0, which is not a bar width
        report = bar_gap_report(pd.Index([1_700_000_000_000] * 5))

        assert report["gap_count"] == 0

    def test_a_skipped_bar_is_counted_and_logged(self):
        from src.features.pipeline import _warn_on_bar_gaps, bar_gap_report

        step = 900_000
        index = pd.Index([1_700_000_000_000 + i * step for i in range(10)])
        gapped = index.delete(5)

        report = bar_gap_report(gapped)
        assert report["gap_count"] == 1
        assert report["missing_bars"] == 1

        _warn_on_bar_gaps(gapped)  # logs, never raises


# ---------------------------------------------------------------------------
# Router: TWAP fallback
# ---------------------------------------------------------------------------


def test_an_unknown_algorithm_falls_through_to_twap():
    from src.execution.router import SmartOrderRouter

    router = SmartOrderRouter(exchanges=["binance"])
    router._select_algo = MagicMock(return_value="twap")
    router._twap = AsyncMock(return_value="twap-result")
    router._get_exchange = MagicMock(return_value=MagicMock())

    result = asyncio.run(
        router.route({"symbol": "BTC/USDT", "side": "buy", "price": 50_000.0}, 0.01, 5_000.0)
    )

    assert result == "twap-result"
    router._twap.assert_awaited_once()


# ---------------------------------------------------------------------------
# Runtime monitor: a probe wrapper that itself fails
# ---------------------------------------------------------------------------


def test_a_probe_wrapper_failure_is_recorded_as_a_failed_probe():
    from src.diagnostics.runtime_monitor import RuntimeMonitor

    monitor = RuntimeMonitor()
    monitor.register_probe("storage", AsyncMock(return_value=True))

    async def _run():
        with patch.object(monitor, "_run_probe", side_effect=RuntimeError("gather wrapper broke")):
            await monitor._run_all_probes()

    asyncio.run(_run())

    result = monitor._results["storage"]
    assert result.passed is False
    assert "probe_wrapper_failed" in result.detail


# ---------------------------------------------------------------------------
# ECDSA scan
# ---------------------------------------------------------------------------


def test_a_truncated_der_signature_is_rejected():
    from src.ecc.ecdsa_scan import _parse_der_signature

    assert _parse_der_signature(b"\x30\x44\x02\x20") is None


def test_the_r_registry_evicts_its_oldest_entry_when_full():
    """The registry is bounded: tracking every r seen would grow forever."""
    from src.ecc import ecdsa_scan as mod

    scanner = mod.ECDSAScanner(max_tracked_r=1)
    pubkey = b"\x02" + b"\x11" * 32

    def _sigs_for(r: int):
        return [(r, 5555, pubkey, f"tx{r}")]

    for r in (1111, 2222):
        with patch.object(mod, "extract_ecdsa_signatures", return_value=_sigs_for(r)):
            scanner.scan_transaction("00", tx_hash_z=42)

    assert list(scanner._r_registry) == [2222]
    assert scanner._evicted_r == 1


def test_a_reused_r_value_is_reported_as_nonce_reuse():
    from src.ecc import ecdsa_scan as mod

    scanner = mod.ECDSAScanner()
    pubkey = b"\x02" + b"\x11" * 32
    found = []
    for i, s_val in enumerate((11111, 22222)):
        with patch.object(
            mod,
            "extract_ecdsa_signatures",
            return_value=[(99999, s_val, pubkey, f"tx{i}")],
        ):
            found.extend(scanner.scan_transaction("00", tx_hash_z=1234 + i))

    assert found  # same r twice means the same k was used
