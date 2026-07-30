"""Tests for src/config.py validators and settings edge cases not already
covered by tests/test_config_feature_settings.py or the risk-controls tests."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.config import (
    APISettings,
    BinanceSettings,
    FeatureSettings,
    HMMSettings,
    RiskSettings,
    RuntimeConfig,
    Settings,
    Timeframe,
    TradingMode,
    get_settings,
    invalidate_settings_cache,
)


class TestBinanceSettingsResolveUrls:
    def test_testnet_true_leaves_default_testnet_urls(self):
        cfg = BinanceSettings(testnet=True)
        assert cfg.base_url == "https://testnet.binance.vision"
        assert cfg.ws_url == "wss://testnet.binance.vision/ws"

    def test_testnet_false_injects_production_urls_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = BinanceSettings(testnet=False)
        assert cfg.base_url == "https://api.binance.com"
        assert cfg.ws_url == "wss://stream.binance.com:9443/ws"

    def test_testnet_false_respects_env_override_for_base_url(self):
        with patch.dict(os.environ, {"BINANCE_BASE_URL": "https://custom-proxy.example.com"}):
            cfg = BinanceSettings(testnet=False)
        assert cfg.base_url == "https://custom-proxy.example.com"

    def test_testnet_false_respects_env_override_for_ws_url(self):
        with patch.dict(os.environ, {"BINANCE_WS_URL": "wss://custom-proxy.example.com/ws"}):
            cfg = BinanceSettings(testnet=False)
        assert cfg.ws_url == "wss://custom-proxy.example.com/ws"


class TestHMMSettingsValidateCovType:
    def test_valid_covariance_type_accepted(self):
        cfg = HMMSettings(covariance_type="diag")
        assert cfg.covariance_type == "diag"

    def test_invalid_covariance_type_raises(self):
        with pytest.raises(ValidationError, match="covariance_type must be one of"):
            HMMSettings(covariance_type="not-a-real-type")


class TestAPISettingsCorsOrigins:
    def test_valid_origins_accepted(self):
        cfg = APISettings(cors_origins=["https://app.example.com"])
        assert cfg.cors_origins == ["https://app.example.com"]

    def test_wildcard_origin_raises(self):
        with pytest.raises(ValidationError, match="CORS wildcard"):
            APISettings(cors_origins=["*"])


class TestSettingsValidateLogLevel:
    def test_valid_log_level_normalized_uppercase(self):
        cfg = Settings(log_level="debug")
        assert cfg.log_level == "DEBUG"

    def test_invalid_log_level_raises(self):
        with pytest.raises(ValidationError, match="log_level must be one of"):
            Settings(log_level="NOT_A_LEVEL")


class TestSettingsEnforceLiveGate:
    def test_live_mode_without_env_var_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValidationError, match="TRADING_MODE=live in environment"):
                Settings(trading_mode=TradingMode.LIVE)

    def test_live_mode_with_env_var_set_succeeds(self):
        with patch.dict(os.environ, {"TRADING_MODE": "live"}):
            cfg = Settings(trading_mode=TradingMode.LIVE)
        assert cfg.trading_mode == TradingMode.LIVE

    def test_live_mode_env_var_case_and_whitespace_normalized(self):
        with patch.dict(os.environ, {"TRADING_MODE": "  LIVE  "}):
            cfg = Settings(trading_mode=TradingMode.LIVE)
        assert cfg.trading_mode == TradingMode.LIVE


class TestSettingsValidatePrimaryTimeframe:
    def test_primary_timeframe_in_active_succeeds(self):
        cfg = Settings(
            active_timeframes=[Timeframe.INTRADAY, Timeframe.SWING],
            primary_timeframe=Timeframe.INTRADAY,
        )
        assert cfg.primary_timeframe == Timeframe.INTRADAY

    def test_primary_timeframe_not_in_active_raises(self):
        with pytest.raises(ValidationError, match="must be in active_timeframes"):
            Settings(
                active_timeframes=[Timeframe.SWING],
                primary_timeframe=Timeframe.SCALPING,
            )


class TestFeatureSettingsValidatePurgeGap:
    def test_purge_gap_covers_horizon_succeeds(self):
        cfg = FeatureSettings(purge_gap_bars=10, triple_barrier_max_holding_bars=5)
        assert cfg.purge_gap_bars == 10

    def test_purge_gap_shorter_than_horizon_raises(self):
        with pytest.raises(ValidationError, match="must be >="):
            FeatureSettings(purge_gap_bars=2, triple_barrier_max_holding_bars=10)


def test_invalidate_settings_cache_forces_reconstruction():
    first = get_settings()
    invalidate_settings_cache()
    second = get_settings()
    assert first is not second  # cache cleared -> new instance built


def test_runtime_config_sync_execution_mode_property():
    rc = RuntimeConfig()
    assert rc.execution_mode == rc._execution_mode


class TestFeatureSettingsGARCH:
    def test_garch_window_default_is_100(self):
        cfg = FeatureSettings()
        assert cfg.garch_window == 100

    def test_garch_window_minimum_is_50(self):
        with pytest.raises(ValidationError):
            FeatureSettings(garch_window=49)

    def test_garch_window_custom_value(self):
        cfg = FeatureSettings(garch_window=200)
        assert cfg.garch_window == 200


class TestRiskSettingsGARCHVolThreshold:
    def test_garch_vol_threshold_default_is_002(self):
        cfg = RiskSettings()
        assert cfg.garch_vol_threshold == pytest.approx(0.02)

    def test_garch_vol_threshold_must_be_positive(self):
        with pytest.raises(ValidationError):
            RiskSettings(garch_vol_threshold=0.0)

    def test_garch_vol_threshold_max_is_050(self):
        with pytest.raises(ValidationError):
            RiskSettings(garch_vol_threshold=0.51)

    def test_garch_vol_threshold_custom_value(self):
        cfg = RiskSettings(garch_vol_threshold=0.05)
        assert cfg.garch_vol_threshold == pytest.approx(0.05)
