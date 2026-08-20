"""
Detection-path coverage for E-16 (adversarial) and E-17 (liquidity stress).

Both engines are safety rails rather than signal sources: E-16 decides whether
volume can be trusted at all, E-17 decides where a bid cascade would stop. The
existing suite only reached their abstain guards.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def make_ohlcv(n: int = 120, seed: int = 21) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = np.cumprod(1 + rng.normal(0.0, 0.01, n)) * 50_000.0
    return pd.DataFrame(
        {
            "timestamp_utc": pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC"),
            "open": closes * 0.999,
            "high": closes * 1.004,
            "low": closes * 0.996,
            "close": closes,
            "volume": rng.uniform(1000, 5000, n),
        }
    )


# ---------------------------------------------------------------------------
# E-16 Adversarial
# ---------------------------------------------------------------------------


def _events(wall_cancel_ms: int) -> list[dict]:
    """20 ordinary quotes plus one outsized wall with the given cancel latency."""
    events = [{"size": 1.0 + 0.01 * i, "cancelled_ms": 5_000} for i in range(20)]
    events.append({"size": 500.0, "cancelled_ms": wall_cancel_ms})
    return events


class TestE16:
    def test_no_events_means_no_spoofing(self) -> None:
        from src.engines.e16_adversarial import spoof_confidence

        assert spoof_confidence([]) == 0.0

    def test_uniform_sizes_have_no_detectable_wall(self) -> None:
        from src.engines.e16_adversarial import spoof_confidence

        assert spoof_confidence([{"size": 1.0} for _ in range(20)]) == 0.0

    def test_a_wall_held_past_the_window_is_not_spoofing(self) -> None:
        from src.engines.e16_adversarial import spoof_confidence

        assert spoof_confidence(_events(wall_cancel_ms=5_000)) == 0.0

    def test_a_wall_cancelled_inside_the_window_is_spoofing(self) -> None:
        from src.engines.e16_adversarial import spoof_confidence

        assert spoof_confidence(_events(wall_cancel_ms=100)) == 1.0

    def test_walls_below_the_sigma_threshold_are_ignored(self) -> None:
        from src.engines.e16_adversarial import spoof_confidence

        rng = np.random.default_rng(2)
        events = [{"size": float(s), "cancelled_ms": 10} for s in rng.uniform(1.0, 2.0, 50)]
        assert spoof_confidence(events) == 0.0

    def test_benford_deviation_needs_thirty_positive_sizes(self) -> None:
        from src.engines.e16_adversarial import benford_deviation

        assert benford_deviation(np.array([1.0, 2.0, 3.0])) == 0.0
        assert benford_deviation(np.zeros(100)) == 0.0

    def test_benford_deviation_is_small_for_a_natural_size_distribution(self) -> None:
        from src.engines.e16_adversarial import benford_deviation

        rng = np.random.default_rng(9)
        sizes = 10 ** rng.uniform(0, 4, 4000)  # log-uniform ⇒ Benford by construction
        assert benford_deviation(sizes) < 0.15

    def test_benford_deviation_is_large_when_every_size_starts_with_nine(self) -> None:
        from src.engines.e16_adversarial import benford_deviation

        assert benford_deviation(np.full(200, 9.5)) > 0.15

    @pytest.mark.asyncio
    async def test_spoofing_drops_confidence_and_raises_the_manipulation_flag(self) -> None:
        from src.engines.e16_adversarial import E16Adversarial

        out = await E16Adversarial().run(
            "BTC/USDT", {"spot": 50_000.0, "orderbook_events": _events(100)}
        )

        assert out.confidence == 0.0
        assert out.direction == 0  # E-16 never takes a side
        assert out.metadata["manipulation_flag"] is True
        assert out.metadata["spoof_confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_a_clean_book_keeps_full_confidence_and_volume_trust(self) -> None:
        from src.engines.e16_adversarial import E16Adversarial

        rng = np.random.default_rng(9)
        data = {
            "spot": 50_000.0,
            "orderbook_events": _events(5_000),
            "trade_sizes": (10 ** rng.uniform(0, 4, 4000)).tolist(),
        }
        out = await E16Adversarial().run("BTC/USDT", data)

        assert out.confidence == 1.0
        assert out.metadata["manipulation_flag"] is False
        assert out.metadata["volume_trust_score"] > 0.0

    @pytest.mark.asyncio
    async def test_wash_traded_sizes_raise_the_manipulation_flag(self) -> None:
        from src.engines.e16_adversarial import E16Adversarial

        out = await E16Adversarial().run(
            "BTC/USDT", {"spot": 50_000.0, "trade_sizes": np.full(200, 9.5).tolist()}
        )

        assert out.metadata["manipulation_flag"] is True
        assert out.metadata["volume_trust_score"] == 0.0

    @pytest.mark.asyncio
    async def test_malformed_events_abstain(self) -> None:
        from src.engines.e16_adversarial import E16Adversarial

        out = await E16Adversarial().run(
            "BTC/USDT", {"spot": 50_000.0, "orderbook_events": [{"size": "big"}] * 40}
        )
        assert out.confidence == 0.0
        assert out.direction == 0


# ---------------------------------------------------------------------------
# E-17 Liquidity
# ---------------------------------------------------------------------------


def _bids(spot: float = 50_000.0, levels: int = 20, size: float = 5.0) -> list[dict]:
    return [{"price": spot - i * 10.0, "size": size} for i in range(levels)]


class TestE17:
    def test_kyle_lambda_needs_five_observations(self) -> None:
        from src.engines.e17_liquidity import kyle_lambda

        assert kyle_lambda(np.zeros(3), np.zeros(3)) == 0.0

    def test_kyle_lambda_is_positive_when_volume_pushes_price(self) -> None:
        from src.engines.e17_liquidity import kyle_lambda

        signed = np.array([-3.0, -1.0, 0.0, 1.0, 3.0, 5.0])
        assert kyle_lambda(signed * 0.001, signed) > 0.0

    def test_amihud_ratio_needs_five_observations(self) -> None:
        from src.engines.e17_liquidity import amihud_ratio

        assert amihud_ratio(np.zeros(3), np.ones(3)) == 0.0

    def test_amihud_ratio_rises_as_volume_thins(self) -> None:
        from src.engines.e17_liquidity import amihud_ratio

        returns = np.full(20, 0.01)
        thin = amihud_ratio(returns, np.full(20, 10.0))
        deep = amihud_ratio(returns, np.full(20, 10_000.0))
        assert thin > deep > 0.0

    def test_depth_score_is_zero_without_bids_or_spot(self) -> None:
        from src.engines.e17_liquidity import depth_score

        assert depth_score([], 50_000.0) == 0.0
        assert depth_score(_bids(), 0.0) == 0.0

    def test_depth_score_counts_only_bids_inside_the_band(self) -> None:
        from src.engines.e17_liquidity import depth_score

        # 50 bps of 50_000 is 250, so only the first 26 levels (10 apart) qualify.
        assert depth_score(_bids(levels=40), 50_000.0, n_bps=50) == pytest.approx(26 * 5.0)

    def test_cascade_level_falls_back_when_inputs_are_missing(self) -> None:
        from src.engines.e17_liquidity import cascade_price_level

        assert cascade_price_level([], 50_000.0, 10.0) == pytest.approx(49_000.0)
        assert cascade_price_level(_bids(), 50_000.0, 0.0) == pytest.approx(49_000.0)

    def test_cascade_level_is_the_price_where_depth_is_exhausted(self) -> None:
        from src.engines.e17_liquidity import cascade_price_level

        # Two levels of 5.0 cover a 10.0 threshold → the second-best bid.
        assert cascade_price_level(_bids(), 50_000.0, 10.0) == pytest.approx(49_990.0)

    def test_cascade_level_is_floored_at_minus_ten_percent(self) -> None:
        from src.engines.e17_liquidity import cascade_price_level

        deep_bids = [{"price": 20_000.0, "size": 100.0}]
        assert cascade_price_level(deep_bids, 50_000.0, 10.0) == pytest.approx(45_000.0)

    def test_cascade_level_falls_back_when_the_bid_wall_is_too_thin(self) -> None:
        from src.engines.e17_liquidity import cascade_price_level

        assert cascade_price_level(_bids(levels=2), 50_000.0, 1_000.0) == pytest.approx(49_000.0)

    @pytest.mark.asyncio
    async def test_a_liquid_book_scores_high_and_raises_no_stress_flag(self) -> None:
        from src.engines.e17_liquidity import E17Liquidity

        df = make_ohlcv()
        data = {"ohlcv": df, "spot": float(df["close"].iloc[-1]), "bids": _bids()}
        out = await E17Liquidity().run("BTC/USDT", data)

        assert out.direction == 0  # liquidity stress is not directional
        assert 0.0 <= out.metadata["liquidity_score"] <= 1.0
        assert not out.metadata["stress_flag"]
        assert out.metadata["depth_score"] > 0.0
        assert out.metadata["cascade_price_level"] > 0.0

    @pytest.mark.asyncio
    async def test_a_missing_volume_column_abstains(self) -> None:
        from src.engines.e17_liquidity import E17Liquidity

        df = make_ohlcv().drop(columns=["volume"])
        out = await E17Liquidity().run("BTC/USDT", {"ohlcv": df, "spot": 50_000.0})
        assert out.confidence == 0.0
        assert out.direction == 0
