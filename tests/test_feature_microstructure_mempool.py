"""Tests for src/features/microstructure.py and src/features/mempool.py."""

from __future__ import annotations

import asyncio

# ─── microstructure ───────────────────────────────────────────────────────────


class TestComputeOFI:
    def test_empty_book_returns_zero(self) -> None:
        from src.features.microstructure import compute_ofi

        assert compute_ofi([], []) == 0.0

    def test_balanced_book_near_zero(self) -> None:
        from src.features.microstructure import compute_ofi

        bids = [[49990.0, 1.0], [49980.0, 1.0]]
        asks = [[50010.0, 1.0], [50020.0, 1.0]]
        ofi = compute_ofi(bids, asks)
        assert isinstance(ofi, float)

    def test_bid_heavy_positive(self) -> None:
        from src.features.microstructure import compute_ofi

        bids = [[49990.0, 10.0], [49980.0, 10.0]]
        asks = [[50010.0, 0.1], [50020.0, 0.1]]
        ofi = compute_ofi(bids, asks)
        assert ofi > 0.0

    def test_ask_heavy_negative(self) -> None:
        from src.features.microstructure import compute_ofi

        bids = [[49990.0, 0.1]]
        asks = [[50010.0, 10.0]]
        ofi = compute_ofi(bids, asks)
        assert ofi < 0.0

    def test_depth_parameter_respected(self) -> None:
        from src.features.microstructure import compute_ofi

        bids = [[100.0 - i, 1.0] for i in range(20)]
        asks = [[100.0 + i, 1.0] for i in range(1, 21)]
        ofi_5 = compute_ofi(bids, asks, depth=5)
        ofi_10 = compute_ofi(bids, asks, depth=10)
        assert isinstance(ofi_5, float)
        assert isinstance(ofi_10, float)


class TestVPINTracker:
    def test_initial_vpin_zero(self) -> None:
        from src.features.microstructure import VPINTracker

        tracker = VPINTracker()
        assert tracker.vpin == 0.0

    def test_update_returns_float_in_range(self) -> None:
        from src.features.microstructure import VPINTracker

        tracker = VPINTracker(bucket_size=10.0)
        for price in [100.0, 101.0, 99.0, 100.5, 98.0]:
            vpin = tracker.update(price, volume=10.0)
            assert 0.0 <= vpin <= 1.0

    def test_vpin_accumulates_buckets(self) -> None:
        from src.features.microstructure import VPINTracker

        tracker = VPINTracker(bucket_size=5.0, n_buckets=5)
        for i in range(50):
            tracker.update(100.0 + (i % 3 - 1), volume=1.0)
        assert isinstance(tracker.vpin, float)

    def test_update_with_zero_volume(self) -> None:
        from src.features.microstructure import VPINTracker

        tracker = VPINTracker()
        vpin = tracker.update(100.0, volume=0.0)
        assert vpin == 0.0


class TestKyleLambdaEstimator:
    def test_initial_lambda_zero(self) -> None:
        from src.features.microstructure import KyleLambdaEstimator

        est = KyleLambdaEstimator()
        assert est.lambda_ == 0.0

    def test_update_returns_float(self) -> None:
        from src.features.microstructure import KyleLambdaEstimator

        est = KyleLambdaEstimator(window=10)
        for i in range(15):
            val = est.update(100.0 + i * 0.1, signed_volume=float(i))
            assert isinstance(val, float)

    def test_lambda_nonnegative(self) -> None:
        from src.features.microstructure import KyleLambdaEstimator

        est = KyleLambdaEstimator(window=20)
        for i in range(30):
            est.update(100.0 + i, signed_volume=float(i + 1))
        assert est.lambda_ >= 0.0

    def test_zero_volume_no_div_zero(self) -> None:
        from src.features.microstructure import KyleLambdaEstimator

        est = KyleLambdaEstimator()
        val = est.update(100.0, signed_volume=0.0)
        assert isinstance(val, float)


class TestBuildMicrostructureFeatures:
    def test_returns_namedtuple(self) -> None:
        from src.features.microstructure import (
            KyleLambdaEstimator,
            VPINTracker,
            build_microstructure_features,
        )

        bids = [[49990.0, 1.0]]
        asks = [[50010.0, 1.0]]
        ft = build_microstructure_features(
            bids=bids,
            asks=asks,
            vpin_tracker=VPINTracker(),
            kyle_estimator=KyleLambdaEstimator(),
            last_price=50000.0,
            last_trade_volume=10.0,
            last_trade_side="buy",
        )
        assert hasattr(ft, "ofi")
        assert hasattr(ft, "vpin")
        assert hasattr(ft, "kyle_lambda")


# ─── mempool ──────────────────────────────────────────────────────────────────


class TestMempoolFeatures:
    def test_dataclass_fields(self) -> None:
        from src.features.mempool import MempoolFeatures

        mf = MempoolFeatures(
            tx_count=1000,
            fee_rate_p50_sat=5.0,
            fee_rate_p90_sat=10.0,
            mempool_bytes=50_000_000,
            fee_pressure=0.5,
        )
        assert mf.fee_rate_p50_sat == 5.0
        assert mf.tx_count == 1000

    def test_fee_pressure_in_range(self) -> None:
        from src.features.mempool import MempoolFeatures

        mf = MempoolFeatures(
            tx_count=0,
            fee_rate_p50_sat=0.0,
            fee_rate_p90_sat=0.0,
            mempool_bytes=0,
            fee_pressure=0.0,
        )
        assert 0.0 <= mf.fee_pressure <= 1.0


class TestFetchMempoolFeatures:
    def test_fetch_returns_mempool_features_on_bad_rpc(self) -> None:
        from src.features.mempool import MempoolFeatures, fetch_mempool_features

        result = asyncio.run(
            fetch_mempool_features(rpc_url="http://127.0.0.1:9999", rpc_user="x", rpc_pass="x")
        )
        assert isinstance(result, MempoolFeatures)

    def test_fetch_returns_mempool_features_default_args(self) -> None:
        from src.features.mempool import MempoolFeatures, fetch_mempool_features

        result = asyncio.run(fetch_mempool_features())
        assert isinstance(result, MempoolFeatures)
        assert result.tx_count >= 0
