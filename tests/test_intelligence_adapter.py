"""Tests for src/intelligence/intelligence_adapter.py (IntelligenceAdapter)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


class TestIntelligenceAdapter:
    def _make_adapter(self, signal=None):
        from src.intelligence.intelligence_adapter import IntelligenceAdapter

        intel = MagicMock()
        intel.on_bar = AsyncMock(return_value=signal)

        storage = MagicMock()
        storage.store_intelligence_features = AsyncMock()

        return IntelligenceAdapter(intel, storage), intel, storage

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_on_bar_none_signal_skips_storage(self) -> None:
        adapter, _intel, storage = self._make_adapter(signal=None)
        self._run(adapter.on_bar("BTC/USDT", "1h", 1000, {"close": 50000.0, "volume": 10.0}))
        storage.store_intelligence_features.assert_not_called()

    def test_on_bar_with_signal_calls_storage(self) -> None:
        from src.intel import IntelSignal

        sig = IntelSignal(
            symbol="BTC/USDT",
            direction=1,
            size_pct=0.02,
            confidence=0.75,
            horizon_idx=2,
            algo="IOC",
            ecc_anomaly=0.3,
            conflict=False,
            regime_id=1,
            meta={"agreement_ratio": 0.8, "kyle_lambda": 0.001, "ofi": 0.5, "vpin": 0.3},
        )
        adapter, _intel, storage = self._make_adapter(signal=sig)
        self._run(adapter.on_bar("BTC/USDT", "1h", 1000, {"close": 50000.0, "volume": 10.0}))
        storage.store_intelligence_features.assert_called_once()

    def test_on_bar_intel_exception_does_not_crash(self) -> None:
        from src.intelligence.intelligence_adapter import IntelligenceAdapter

        intel = MagicMock()
        intel.on_bar = AsyncMock(side_effect=RuntimeError("boom"))
        storage = MagicMock()
        storage.store_intelligence_features = AsyncMock()

        adapter = IntelligenceAdapter(intel, storage)
        self._run(adapter.on_bar("BTC/USDT", "1h", 1000, {"close": 50000.0, "volume": 10.0}))
        storage.store_intelligence_features.assert_not_called()

    def test_on_bar_storage_exception_does_not_crash(self) -> None:
        from src.intel import IntelSignal

        sig = IntelSignal(
            symbol="BTC/USDT",
            direction=1,
            size_pct=0.02,
            confidence=0.75,
            horizon_idx=2,
            algo="IOC",
            ecc_anomaly=0.3,
            conflict=False,
            regime_id=1,
        )
        adapter, _intel, storage = self._make_adapter(signal=sig)
        storage.store_intelligence_features = AsyncMock(side_effect=OSError("db error"))
        self._run(adapter.on_bar("BTC/USDT", "1h", 1000, {"close": 50000.0, "volume": 10.0}))
        # Should log warning but not raise

    def test_signal_to_features_returns_floats(self) -> None:
        from src.intel import IntelSignal
        from src.intelligence.intelligence_adapter import IntelligenceAdapter

        sig = IntelSignal(
            symbol="ETH/USDT",
            direction=-1,
            size_pct=0.01,
            confidence=0.72,
            horizon_idx=5,
            algo="TWAP",
            ecc_anomaly=0.0,
            conflict=True,
            regime_id=2,
            meta={"agreement_ratio": 0.6, "kyle_lambda": 0.002, "ofi": -0.3, "vpin": 0.5},
        )
        intel = MagicMock()
        storage = MagicMock()
        adapter = IntelligenceAdapter(intel, storage)
        features = adapter._signal_to_features(sig)
        assert isinstance(features, dict)
        for k, v in features.items():
            assert isinstance(v, float), f"{k}={v!r} is not float"

    def test_signal_to_features_algo_flags(self) -> None:
        from src.intel import IntelSignal
        from src.intelligence.intelligence_adapter import IntelligenceAdapter

        intel = MagicMock()
        storage = MagicMock()
        adapter = IntelligenceAdapter(intel, storage)

        for algo, flag_key in [
            ("IOC", "algo_ioc_v6"),
            ("iceberg", "algo_iceberg_v6"),
            ("TWAP", "algo_twap_v6"),
        ]:
            sig = IntelSignal(
                symbol="BTC/USDT",
                direction=1,
                size_pct=0.01,
                confidence=0.7,
                horizon_idx=0,
                algo=algo,
                ecc_anomaly=0.0,
                conflict=False,
                regime_id=0,
            )
            features = adapter._signal_to_features(sig)
            assert features.get(flag_key) == 1.0, f"{algo}: expected {flag_key}=1.0"
