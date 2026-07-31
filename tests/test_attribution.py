"""Tests for src/diagnostics/attribution.py"""

from __future__ import annotations

import math

import pytest

from src.data.storage import TradeRecord
from src.diagnostics.attribution import (
    SliceStats,
    _compute_stats,
    _sharpe,
    build_attribution,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trade(
    pnl_usd: float | None = 10.0,
    pnl_pct: float | None = 0.01,
    regime: int = 1,
    timeframe: str = "15m",
    direction: int = 1,
    raw_signal: float | None = 0.7,
    meta_label_prob: float = 0.6,
    notional_usd: float = 1000.0,
    exit_price: float | None = 101.0,
) -> TradeRecord:
    return TradeRecord(
        id="t1",
        symbol="BTC/USDT",
        timeframe=timeframe,
        trading_mode="paper",
        execution_mode="AUTOMATIC",
        direction=direction,
        entry_price=100.0,
        exit_price=exit_price,
        quantity=0.01,
        notional_usd=notional_usd,
        entry_ts=1_700_000_000,
        exit_ts=1_700_003_600,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        fee_usd=0.5,
        kelly_fraction=0.02,
        regime_at_entry=regime,
        meta_label_prob=meta_label_prob,
        exit_reason="stop_loss",
        approved_by=None,
        raw_signal=raw_signal,
    )


# ---------------------------------------------------------------------------
# _sharpe
# ---------------------------------------------------------------------------


def test_sharpe_empty():
    assert _sharpe([]) == 0.0


def test_sharpe_single():
    assert _sharpe([100.0]) == 0.0


def test_sharpe_zero_std():
    # All same returns → std == 0 → return 0.0 not inf
    assert _sharpe([5.0, 5.0, 5.0]) == 0.0


def test_sharpe_positive():
    pnl = [10.0, 12.0, 8.0, 11.0, 9.0]
    result = _sharpe(pnl)
    assert result > 0.0
    assert math.isfinite(result)


def test_sharpe_negative():
    pnl = [-10.0, -12.0, -8.0, -11.0, -9.0]
    result = _sharpe(pnl)
    assert result < 0.0


# ---------------------------------------------------------------------------
# _compute_stats
# ---------------------------------------------------------------------------


def test_compute_stats_empty():
    s = _compute_stats([])
    assert s.n_trades == 0
    assert s.win_rate == 0.0
    assert s.sharpe == 0.0


def test_compute_stats_all_wins():
    trades = [_trade(pnl_usd=10.0, pnl_pct=0.01) for _ in range(5)]
    s = _compute_stats(trades)
    assert s.n_trades == 5
    assert s.n_wins == 5
    assert s.win_rate == 1.0
    assert s.total_pnl_usd == pytest.approx(50.0)
    assert s.expectancy_usd > 0


def test_compute_stats_mixed():
    trades = [
        _trade(pnl_usd=20.0, pnl_pct=0.02),
        _trade(pnl_usd=-10.0, pnl_pct=-0.01),
    ]
    s = _compute_stats(trades)
    assert s.n_trades == 2
    assert s.n_wins == 1
    assert s.win_rate == pytest.approx(0.5)
    assert s.total_pnl_usd == pytest.approx(10.0)
    assert s.expectancy_usd == pytest.approx(5.0)  # 20*0.5 - 10*0.5


def test_compute_stats_skips_open_trades():
    # open trade has exit_price=None AND pnl_usd=None
    trades = [
        _trade(pnl_usd=10.0, pnl_pct=0.01, exit_price=101.0),
        _trade(pnl_usd=None, pnl_pct=None, exit_price=None),
    ]
    s = _compute_stats(trades)
    # n_trades counts only closed trades (those with pnl_usd)
    assert s.n_trades == 1
    assert s.n_wins == 1


# ---------------------------------------------------------------------------
# build_attribution
# ---------------------------------------------------------------------------


def test_build_attribution_empty():
    report = build_attribution([])
    assert report.n_total == 0
    assert report.n_closed == 0
    assert report.total_pnl_usd == 0.0
    assert report.by_regime == {}


def test_build_attribution_open_only():
    trades = [_trade(exit_price=None, pnl_usd=None)]
    report = build_attribution(trades)
    assert report.n_total == 1
    assert report.n_closed == 0
    assert report.by_regime == {}


def test_build_attribution_single_closed():
    trades = [_trade(pnl_usd=15.0, pnl_pct=0.015, regime=1)]
    report = build_attribution(trades)
    assert report.n_total == 1
    assert report.n_closed == 1
    assert report.total_pnl_usd == pytest.approx(15.0)

    # regime slice
    assert "trending" in report.by_regime
    s = report.by_regime["trending"]
    assert s.n_trades == 1
    assert s.n_wins == 1
    assert s.total_pnl_usd == pytest.approx(15.0)


def test_build_attribution_by_regime_labels():
    trades = [
        _trade(regime=0, pnl_usd=5.0),
        _trade(regime=1, pnl_usd=10.0),
        _trade(regime=2, pnl_usd=-3.0),
    ]
    report = build_attribution(trades)
    assert set(report.by_regime.keys()) == {"ranging", "trending", "volatile"}


def test_build_attribution_unknown_regime():
    trades = [_trade(regime=99, pnl_usd=1.0)]
    report = build_attribution(trades)
    assert "regime_99" in report.by_regime


def test_build_attribution_by_timeframe():
    trades = [
        _trade(timeframe="1m", pnl_usd=1.0),
        _trade(timeframe="15m", pnl_usd=2.0),
        _trade(timeframe="4h", pnl_usd=3.0),
    ]
    report = build_attribution(trades)
    assert "1m" in report.by_timeframe
    assert "15m" in report.by_timeframe
    assert "4h" in report.by_timeframe


def test_build_attribution_by_direction():
    trades = [
        _trade(direction=1, pnl_usd=10.0),
        _trade(direction=0, pnl_usd=-5.0),
    ]
    report = build_attribution(trades)
    assert "long" in report.by_direction
    assert "short" in report.by_direction
    assert report.by_direction["long"].n_wins == 1
    assert report.by_direction["short"].n_wins == 0


def test_build_attribution_confidence_quartiles():
    # 8 trades with different raw_signal values
    trades = [_trade(raw_signal=float(i) / 8.0, pnl_usd=float(i)) for i in range(8)]
    report = build_attribution(trades)
    # Should have up to 4 quartile buckets
    assert len(report.by_confidence_quartile) <= 4
    assert len(report.by_confidence_quartile) >= 1
    # All quartile labels should be from the defined set
    for k in report.by_confidence_quartile:
        assert k in ("Q1_low", "Q2_mid_low", "Q3_mid_high", "Q4_high")


def test_build_attribution_confidence_falls_back_to_meta_label():
    # raw_signal=None → should use meta_label_prob
    trades = [_trade(raw_signal=None, meta_label_prob=0.8, pnl_usd=5.0)]
    report = build_attribution(trades)
    assert len(report.by_confidence_quartile) >= 1


def test_attribution_report_to_dict():
    trades = [_trade(pnl_usd=10.0, regime=1)]
    report = build_attribution(trades)
    d = report.to_dict()
    assert "n_total" in d
    assert "n_closed" in d
    assert "total_pnl_usd" in d
    assert "by_regime" in d
    assert "by_timeframe" in d
    assert "by_direction" in d
    assert "by_confidence_quartile" in d


def test_slice_stats_to_dict():
    s = SliceStats(n_trades=5, n_wins=3, total_pnl_usd=50.0, win_rate=0.6)
    d = s.to_dict()
    assert d["n_trades"] == 5
    assert d["win_rate"] == pytest.approx(0.6)
    assert "_pnl_list" not in d


def test_build_attribution_total_pnl_sum():
    trades = [
        _trade(pnl_usd=10.0),
        _trade(pnl_usd=-3.0),
        _trade(pnl_usd=7.0),
    ]
    report = build_attribution(trades)
    assert report.total_pnl_usd == pytest.approx(14.0)


def test_build_attribution_mixed_open_closed():
    trades = [
        _trade(pnl_usd=10.0, exit_price=101.0),
        _trade(pnl_usd=None, exit_price=None),
        _trade(pnl_usd=5.0, exit_price=102.0),
    ]
    report = build_attribution(trades)
    assert report.n_total == 3
    assert report.n_closed == 2
    assert report.total_pnl_usd == pytest.approx(15.0)
