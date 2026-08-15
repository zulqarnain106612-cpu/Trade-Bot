"""Tests for ProbabilisticMetricsAdapter."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.intelligence.probabilistic_adapter import (
    ProbabilisticGateInputs,
    ProbabilisticMetricsAdapter,
)


def _base_metrics() -> dict:
    return {
        "exchange_stress_score": 0.3,
        "exchange_netflow_7d_zscore": -0.5,
        "binance_funding_rate_pct": 0.01,
        "cross_exchange_basis_spread_bps": 5.0,
        "exchange_reserve_ratio": 0.35,
        "whale_buy_sell_ratio": 1.5,
    }


def test_process_returns_probabilistic_gate_inputs() -> None:
    adapter = ProbabilisticMetricsAdapter()
    result = adapter.process(_base_metrics())
    assert isinstance(result, ProbabilisticGateInputs)


def test_process_no_stress_score_returns_none_stress() -> None:
    adapter = ProbabilisticMetricsAdapter()
    metrics = _base_metrics()
    del metrics["exchange_stress_score"]
    result = adapter.process(metrics)
    assert result.exchange_stress_score is None
    assert result.raw_stress_score is None


def test_process_no_whale_ratio_returns_none_whale() -> None:
    adapter = ProbabilisticMetricsAdapter()
    metrics = _base_metrics()
    del metrics["whale_buy_sell_ratio"]
    result = adapter.process(metrics)
    assert result.whale_buy_sell_ratio is None
    assert result.raw_whale_ratio is None


def test_process_stress_model_exception_fails_open() -> None:
    adapter = ProbabilisticMetricsAdapter()
    with patch.object(
        adapter._stress_model,
        "predict_failure_probability",
        side_effect=RuntimeError("model error"),
    ):
        result = adapter.process(_base_metrics())
    assert result.exchange_stress_score is None


def test_process_whale_model_exception_fails_open() -> None:
    adapter = ProbabilisticMetricsAdapter()
    with patch.object(
        adapter._whale_model,
        "estimate_true_ratio",
        side_effect=ValueError("bad data"),
    ):
        result = adapter.process(_base_metrics())
    assert result.whale_buy_sell_ratio is None


def test_process_stress_score_in_valid_range() -> None:
    adapter = ProbabilisticMetricsAdapter()
    result = adapter.process(_base_metrics())
    if result.exchange_stress_score is not None:
        assert 0.0 <= result.exchange_stress_score <= 1.0


def test_process_raw_stress_score_preserved() -> None:
    adapter = ProbabilisticMetricsAdapter()
    metrics = _base_metrics()
    result = adapter.process(metrics)
    if result.raw_stress_score is not None:
        assert result.raw_stress_score == pytest.approx(0.3)


def test_process_raw_whale_ratio_preserved() -> None:
    adapter = ProbabilisticMetricsAdapter()
    result = adapter.process(_base_metrics())
    if result.raw_whale_ratio is not None:
        assert result.raw_whale_ratio == pytest.approx(1.5)


def test_process_low_confidence_whale_gives_none() -> None:
    # Use a very high min_whale_confidence to force a "too uncertain" result.
    adapter = ProbabilisticMetricsAdapter(min_whale_confidence=1.0)
    result = adapter.process(_base_metrics())
    assert result.whale_buy_sell_ratio is None


def test_process_empty_metrics_all_none() -> None:
    adapter = ProbabilisticMetricsAdapter()
    result = adapter.process({})
    assert result.exchange_stress_score is None
    assert result.whale_buy_sell_ratio is None
    assert result.raw_stress_score is None
    assert result.raw_whale_ratio is None


def test_probabilistic_gate_inputs_default_confidences() -> None:
    gate = ProbabilisticGateInputs(
        exchange_stress_score=0.5,
        whale_buy_sell_ratio=1.2,
    )
    assert gate.exchange_stress_confidence == 0.0
    assert gate.whale_ratio_confidence == 0.0
