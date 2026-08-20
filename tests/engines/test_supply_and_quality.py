"""
Per-coin supply modelling (E-10) and the data quality gate's reject paths.

E-10's ETH and LTC branches and the gate's malformed-input branches were both
unreached: the existing suites only drive BTC and well-formed rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.data.quality_gate import DataQualityGate
from src.engines.e10_supply import E10Supply


# ---------------------------------------------------------------------------
# E-10 supply
# ---------------------------------------------------------------------------


class TestE10Compute:
    def test_eth_uses_a_fixed_post_merge_stock_to_flow(self) -> None:
        fair, sf, cycle_pos, dev = E10Supply._compute("ETH", 3_000.0, block_height=0)

        assert sf == pytest.approx(333.0)
        assert cycle_pos == "mid"  # no halving cycle → fixed at 0.5
        assert fair > 0.0
        assert dev == pytest.approx((3_000.0 - fair) / fair * 100)

    def test_ltc_derives_stock_to_flow_from_its_own_halving_schedule(self) -> None:
        fair, sf, cycle_pos, dev = E10Supply._compute("LTC", 100.0, block_height=2_800_000)

        assert sf > 0.0
        assert fair > 0.0
        assert cycle_pos in ("early", "mid", "late")

    def test_ltc_falls_back_to_a_known_height_when_none_is_supplied(self) -> None:
        with_height = E10Supply._compute("LTC", 100.0, block_height=0)
        assert with_height[1] > 0.0

    def test_an_unmodelled_coin_reports_spot_as_its_own_fair_value(self) -> None:
        assert E10Supply._compute("XMR", 150.0, block_height=0) == (150.0, 0.0, "unknown", 0.0)

    @pytest.mark.asyncio
    async def test_a_non_positive_spot_abstains(self) -> None:
        out = await E10Supply().run("BTC/USDT", {"spot": 0.0})
        assert out.confidence == 0.0
        assert out.direction == 0

    @pytest.mark.asyncio
    async def test_the_engine_warms_up_before_it_scores_a_deviation(self) -> None:
        e = E10Supply()
        out = await e.run("ETH/USDT", {"spot": 3_000.0, "block_height": 0})
        assert out.confidence == 0.0  # warming_up

    @pytest.mark.asyncio
    async def test_a_deviation_far_above_its_own_norm_reads_as_rich(self) -> None:
        """Once the history is warm, an outsized deviation should short."""
        e = E10Supply()
        for spot in np.linspace(3_000.0, 3_010.0, 40):
            await e.run("ETH/USDT", {"spot": float(spot), "block_height": 0})

        out = await e.run("ETH/USDT", {"spot": 30_000.0, "block_height": 0})

        assert out.direction == -1
        assert out.confidence > 0.0
        assert out.metadata["deviation_z"] > 0.0


# ---------------------------------------------------------------------------
# Data quality gate
# ---------------------------------------------------------------------------


class TestQualityGateRejects:
    def setup_method(self) -> None:
        self.gate = DataQualityGate()

    def test_an_empty_frame_is_rejected(self) -> None:
        result = self.gate.check_ohlcv(pd.DataFrame())
        assert not result.passed
        assert result.reason == "empty_dataframe"

    def test_macro_without_a_date_is_rejected(self) -> None:
        assert not self.gate.check_macro({}).passed

    def test_macro_with_an_unparseable_date_is_rejected(self) -> None:
        result = self.gate.check_macro({"date": "last tuesday"})
        assert not result.passed
        assert "bad_date_format" in result.reason

    def test_stale_macro_is_rejected(self) -> None:
        old = (datetime.now(UTC) - timedelta(days=5)).date().isoformat()
        assert not self.gate.check_macro({"date": old}).passed

    def test_price_deviation_cannot_be_validated_without_both_sides(self) -> None:
        assert self.gate.check_price_deviation(0.0, 50_000.0).passed
        assert self.gate.check_price_deviation(50_000.0, 0.0).passed

    def test_a_wide_cross_source_deviation_is_rejected(self) -> None:
        result = self.gate.check_price_deviation(50_000.0, 51_000.0)
        assert not result.passed
        assert "cross_source_deviation" in result.reason

    def test_a_tight_cross_source_deviation_passes(self) -> None:
        assert self.gate.check_price_deviation(50_000.0, 50_050.0).passed

    def test_a_sentiment_score_outside_zero_to_one_hundred_is_rejected(self) -> None:
        assert not self.gate.check_sentiment_score(-1.0).passed
        assert not self.gate.check_sentiment_score(101.0).passed

    def test_an_in_range_sentiment_score_passes(self) -> None:
        assert self.gate.check_sentiment_score(78.0).passed

    def test_an_unavailable_audit_sink_does_not_block_validation(self) -> None:
        with patch(
            "src.diagnostics.audit_trail.get_audit_trail", side_effect=RuntimeError("no sink")
        ):
            assert not self.gate.check_sentiment_score(500.0).passed
