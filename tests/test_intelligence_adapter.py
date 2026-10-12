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
        return asyncio.run(coro)

    def test_on_bar_none_signal_skips_storage(self) -> None:
        adapter, _intel, storage = self._make_adapter(signal=None)
        self._run(adapter.on_bar("BTC/USDT", "1h", 1000, {"close": 50000.0, "volume": 10.0}))
        storage.store_intelligence_features.assert_not_called()

    def test_on_bar_persists_supported_v6_decision(self) -> None:
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
        storage.store_intelligence_features.assert_awaited_once()
        fields = storage.store_intelligence_features.await_args.kwargs["features"]
        assert fields["direction_v6"] == 1.0
        assert fields["horizon_idx_v6"] == 2.0

    def test_on_bar_intel_exception_does_not_crash(self) -> None:
        from src.intelligence.intelligence_adapter import IntelligenceAdapter

        intel = MagicMock()
        intel.on_bar = AsyncMock(side_effect=RuntimeError("boom"))
        storage = MagicMock()
        storage.store_intelligence_features = AsyncMock()

        adapter = IntelligenceAdapter(intel, storage)
        self._run(adapter.on_bar("BTC/USDT", "1h", 1000, {"close": 50000.0, "volume": 10.0}))
        storage.store_intelligence_features.assert_not_called()

    def test_on_bar_unsupported_signal_feature_contract_skips_storage(self) -> None:
        from src.intelligence.intelligence_adapter import IntelligenceAdapter

        signal = MagicMock()
        signal.direction = "not-a-number"
        signal.size_pct = 0.02
        signal.confidence = 0.75
        signal.horizon_idx = 2
        signal.ecc_anomaly = 0.3
        signal.conflict = False
        signal.regime_id = 1
        signal.meta = {}

        adapter, _intel, storage = self._make_adapter(signal=signal)
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

    def test_signal_to_features_maps_complete_v6_contract(self) -> None:
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
        assert features["direction_v6"] == -1.0
        assert features["size_pct_v6"] == 0.01
        assert features["confidence_v6"] == 0.72
        assert features["horizon_idx_v6"] == 5.0
        assert features["conflict_v6"] == 1.0
        assert features["algo_v6"] == "TWAP"
        assert features["horizon_contributions_v6"] == "{}"

    def test_v6_storage_round_trip_and_unknown_column_rejection(self) -> None:
        import pandas as pd

        from src.data.storage import StorageBackend

        async def exercise_storage_lifecycle() -> None:
            storage = StorageBackend(db_path=":memory:")
            try:
                await storage.initialize()
                features = {
                    "direction_v6": -1.0,
                    "size_pct_v6": 0.01,
                    "confidence_v6": 0.72,
                    "horizon_idx_v6": 5.0,
                    "ecc_anomaly_v6": None,
                    "conflict_v6": 1.0,
                    "regime_id_v6": 2.0,
                    "agreement_ratio_v6": 0.6,
                    "kyle_lambda_v6": 0.002,
                    "ofi_v6": -0.3,
                    "vpin_v6": 0.5,
                    "expected_return_v6": 0.02,
                    "uncertainty_v6": 0.01,
                    "algo_v6": "TWAP",
                    "horizon_contributions_v6": '{"5":1.0}',
                    "model_versions_v6": '{"5":"v1"}',
                    "freshness_seconds_v6": '{"5":0.2}',
                }
                await storage.store_intelligence_features(
                    "BTC/USDT", "1h", 1000, features, confidence=0.72, source="test"
                )
                rows = await storage.fetch_intelligence_features("BTC/USDT", "1h")
                assert rows.loc[1000, "direction_v6"] == -1
                assert rows.loc[1000, "size_pct_v6"] == 0.01
                assert rows.loc[1000, "horizon_idx_v6"] == 5
                assert pd.isna(rows.loc[1000, "ecc_anomaly_v6"])
                assert rows.loc[1000, "algo_v6"] == "TWAP"
                assert rows.loc[1000, "horizon_contributions_v6"] == '{"5":1.0}'
            finally:
                await storage.close()

            unknown_column_storage = StorageBackend(db_path=":memory:")
            try:
                await unknown_column_storage.initialize()
                try:
                    await unknown_column_storage.store_intelligence_features(
                        "BTC/USDT",
                        "1h",
                        1000,
                        {"direction_v6": 1.0, "unknown": 3.0},
                        confidence=0.7,
                    )
                except ValueError as exc:
                    assert "unsupported intelligence feature columns" in str(exc)
                else:
                    raise AssertionError(
                        "unknown feature names must not be silently stored as NULL"
                    )
            finally:
                await unknown_column_storage.close()

        self._run(exercise_storage_lifecycle())
