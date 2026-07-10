"""Tests for src/features/intelligence_features.py."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.features.intelligence_features import (
    IntelligenceFeatureMatrix,
    IntelligenceMetrics,
    add_intelligence_features,
)


def _make_metrics(**overrides) -> IntelligenceMetrics:
    defaults = {
        "exchange_netflow_7d_zscore": 0.1,
        "whale_buy_sell_ratio": 1.2,
        "exchange_reserve_ratio": 0.15,
        "miner_netflow_signal": 0.3,
        "staking_unlock_risk": 0.05,
        "entity_exchange_imbalance": 0.2,
        "binance_funding_rate_pct": 0.01,
        "liquidation_pressure_24h_zscore": -0.5,
        "futures_oi_change_pct": 0.02,
        "liquidation_cascade_risk_usd": 1_000_000.0,
        "btc_dominance_regime": 0.3,
        "stablecoin_reserve_ratio": 0.08,
        "network_activity_score": 0.6,
        "exchange_stress_score": 0.1,
        "cross_exchange_basis_spread_bps": 5.0,
        "defi_tvl_7d_change_pct": 0.02,
        "mvrv_z_score": 1.5,
        "sopr": 1.01,
        "timestamp": 1_700_000_000,
        "confidence": 0.85,
    }
    defaults.update(overrides)
    return IntelligenceMetrics(**defaults)


def _make_df(n=10) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": np.ones(n) * 100.0,
            "high": np.ones(n) * 101.0,
            "low": np.ones(n) * 99.0,
            "close": np.ones(n) * 100.5,
            "volume": np.ones(n) * 1000.0,
        }
    )


# ---------------------------------------------------------------------------
# IntelligenceMetrics
# ---------------------------------------------------------------------------


def test_intelligence_metrics_to_dict():
    m = _make_metrics()
    d = m.to_dict()
    assert "exchange_netflow_7d_zscore" in d
    assert "whale_buy_sell_ratio" in d
    assert "defi_tvl_7d_change_pct" in d
    assert "mvrv_z_score" in d
    assert "sopr" in d
    # confidence is metadata, not in to_dict
    assert len(d) == 18


def test_intelligence_metrics_defaults():
    m = _make_metrics()
    assert m.defi_tvl_7d_change_pct == 0.02
    assert m.mvrv_z_score == 1.5
    assert m.sopr == 1.01
    assert m.confidence == 0.85


def test_intelligence_metrics_zero_defaults():
    m = IntelligenceMetrics(
        exchange_netflow_7d_zscore=0.0,
        whale_buy_sell_ratio=1.0,
        exchange_reserve_ratio=0.0,
        miner_netflow_signal=0.0,
        staking_unlock_risk=0.0,
        entity_exchange_imbalance=0.0,
        binance_funding_rate_pct=0.0,
        liquidation_pressure_24h_zscore=0.0,
        futures_oi_change_pct=0.0,
        liquidation_cascade_risk_usd=0.0,
        btc_dominance_regime=0.0,
        stablecoin_reserve_ratio=0.0,
        network_activity_score=0.0,
        exchange_stress_score=0.0,
        cross_exchange_basis_spread_bps=0.0,
    )
    assert m.defi_tvl_7d_change_pct == 0.0
    assert m.mvrv_z_score == 0.0
    assert m.sopr == 0.0
    assert m.timestamp == 0
    assert m.confidence == 0.0


# ---------------------------------------------------------------------------
# add_intelligence_features
# ---------------------------------------------------------------------------


def test_add_intelligence_features_empty_df_returns_empty():
    df = pd.DataFrame()
    m = _make_metrics()
    result = add_intelligence_features(df, m)
    assert result.empty


def test_add_intelligence_features_adds_columns():
    df = _make_df()
    m = _make_metrics()
    result = add_intelligence_features(df, m)
    # Columns use 'intelligence_' prefix
    intel_cols = [c for c in result.columns if c.startswith("intelligence_")]
    assert len(intel_cols) >= 18
    assert any("netflow" in c for c in result.columns)
    assert any("whale" in c for c in result.columns)
    assert any("defi_tvl" in c for c in result.columns)


def test_add_intelligence_features_does_not_mutate_input():
    df = _make_df()
    orig_cols = list(df.columns)
    m = _make_metrics()
    add_intelligence_features(df, m)
    assert list(df.columns) == orig_cols


def test_add_intelligence_features_correct_values():
    df = _make_df(5)
    m = _make_metrics(whale_buy_sell_ratio=2.5, confidence=0.9)
    result = add_intelligence_features(df, m)
    whale_col = next(c for c in result.columns if "whale_buy_sell_ratio" in c)
    conf_col = next(c for c in result.columns if "confidence" in c)
    assert (result[whale_col] == 2.5).all()
    assert (result[conf_col] == 0.9).all()


def test_add_intelligence_features_preserves_original_columns():
    df = _make_df()
    m = _make_metrics()
    result = add_intelligence_features(df, m)
    assert "close" in result.columns
    assert "volume" in result.columns


def test_add_intelligence_features_broadcasts_scalar():
    df = _make_df(20)
    m = _make_metrics(exchange_netflow_7d_zscore=-1.5)
    result = add_intelligence_features(df, m)
    assert len(result) == 20
    netflow_col = next(c for c in result.columns if "netflow" in c)
    assert (result[netflow_col] == -1.5).all()


# ---------------------------------------------------------------------------
# IntelligenceFeatureMatrix
# ---------------------------------------------------------------------------


def test_feature_matrix_feature_count():
    df = _make_df()
    fm = IntelligenceFeatureMatrix(X=df)
    assert fm.feature_count == 5  # 5 OHLCV columns


def test_feature_matrix_sample_count():
    df = _make_df(15)
    fm = IntelligenceFeatureMatrix(X=df)
    assert fm.sample_count == 15


def test_feature_matrix_none_x():
    fm = IntelligenceFeatureMatrix(X=None)
    assert fm.feature_count == 0
    assert fm.sample_count == 0


def test_feature_matrix_with_labels():
    df = _make_df()
    y_dir = pd.Series([1, 0, 1, 0, 1, 0, 1, 0, 1, 0])
    y_meta = pd.Series([1, 1, 0, 0, 1, 1, 0, 0, 1, 1])
    m = _make_metrics()
    fm = IntelligenceFeatureMatrix(X=df, y_direction=y_dir, y_meta=y_meta, intelligence_metrics=m)
    assert fm.y_direction is not None
    assert fm.y_meta is not None
    assert fm.intelligence_metrics is not None
