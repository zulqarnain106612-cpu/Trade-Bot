"""Tests for src/risk/slippage.py — Almgren-Chriss slippage/impact model."""

import math

import pytest

from src.config import invalidate_settings_cache
from src.risk.slippage import SlippageEstimate, SlippageModel


@pytest.fixture(autouse=True)
def reset_settings():
    invalidate_settings_cache()
    yield
    invalidate_settings_cache()


# ─── SlippageModel.estimate ───────────────────────────────────────────────────


class TestEstimate:
    def test_basic_estimate_shape(self):
        model = SlippageModel()
        est = model.estimate("BTC/USDT", qty=0.5, price=60_000.0, adv_20d=5_000.0)
        assert isinstance(est, SlippageEstimate)
        assert est.symbol == "BTC/USDT"
        assert est.notional_usd == pytest.approx(30_000.0)
        assert est.participation_rate == pytest.approx(0.5 / 5_000.0)

    def test_default_spread_used_when_not_supplied(self):
        model = SlippageModel()
        est = model.estimate("BTC/USDT", qty=1.0, price=100.0, adv_20d=1000.0)
        assert est.spread_bps == pytest.approx(model._cfg.slippage_default_spread_bps)

    def test_explicit_spread_overrides_default(self):
        model = SlippageModel()
        est = model.estimate("BTC/USDT", qty=1.0, price=100.0, adv_20d=1000.0, spread_bps=7.5)
        assert est.spread_bps == pytest.approx(7.5)

    def test_impact_follows_sqrt_law(self):
        # Almgren-Chriss: impact scales with sqrt(participation), so 4x the
        # participation rate should give exactly 2x the impact component.
        model = SlippageModel()
        small = model.estimate("BTC/USDT", qty=1.0, price=100.0, adv_20d=1000.0)
        large = model.estimate("BTC/USDT", qty=4.0, price=100.0, adv_20d=1000.0)
        assert large.impact_bps == pytest.approx(2.0 * small.impact_bps, rel=1e-9)

    def test_total_slippage_is_spread_plus_impact(self):
        model = SlippageModel()
        est = model.estimate("BTC/USDT", qty=2.0, price=100.0, adv_20d=1000.0)
        assert est.total_slippage_bps == pytest.approx(est.spread_bps + est.impact_bps)

    def test_total_cost_usd_matches_notional_and_bps(self):
        model = SlippageModel()
        est = model.estimate("BTC/USDT", qty=2.0, price=100.0, adv_20d=1000.0)
        expected_cost = est.notional_usd * (est.total_slippage_bps / 10_000.0)
        assert est.total_cost_usd == pytest.approx(expected_cost)

    def test_zero_participation_gives_zero_impact(self):
        # As qty -> 0, sqrt(qty/adv) -> 0, so impact -> 0 (spread-only cost).
        model = SlippageModel()
        est = model.estimate("BTC/USDT", qty=1e-9, price=100.0, adv_20d=1000.0)
        assert est.impact_bps == pytest.approx(0.0, abs=1e-3)

    def test_full_participation_rate_one(self):
        model = SlippageModel()
        est = model.estimate("BTC/USDT", qty=1000.0, price=100.0, adv_20d=1000.0)
        assert est.participation_rate == pytest.approx(1.0)
        assert est.impact_bps == pytest.approx(model._cfg.slippage_impact_coeff_bps)

    @pytest.mark.parametrize("bad_qty", [0.0, -1.0, float("nan"), float("inf")])
    def test_invalid_qty_raises(self, bad_qty):
        model = SlippageModel()
        with pytest.raises(ValueError):
            model.estimate("BTC/USDT", qty=bad_qty, price=100.0, adv_20d=1000.0)

    @pytest.mark.parametrize("bad_price", [0.0, -50.0, float("nan")])
    def test_invalid_price_raises(self, bad_price):
        model = SlippageModel()
        with pytest.raises(ValueError):
            model.estimate("BTC/USDT", qty=1.0, price=bad_price, adv_20d=1000.0)

    @pytest.mark.parametrize("bad_adv", [0.0, -10.0, float("nan")])
    def test_invalid_adv_raises(self, bad_adv):
        model = SlippageModel()
        with pytest.raises(ValueError):
            model.estimate("BTC/USDT", qty=1.0, price=100.0, adv_20d=bad_adv)

    def test_negative_spread_raises(self):
        model = SlippageModel()
        with pytest.raises(ValueError):
            model.estimate("BTC/USDT", qty=1.0, price=100.0, adv_20d=1000.0, spread_bps=-1.0)

    def test_as_dict_rounds_and_contains_all_fields(self):
        model = SlippageModel()
        est = model.estimate("ETH/USDT", qty=3.0, price=2_500.0, adv_20d=10_000.0)
        d = est.as_dict()
        for key in (
            "symbol",
            "qty",
            "notional_usd",
            "adv_20d",
            "spread_bps",
            "impact_bps",
            "total_slippage_bps",
            "total_cost_usd",
            "participation_rate",
        ):
            assert key in d


# ─── SlippageModel.veto_if_negative_ev ────────────────────────────────────────


class TestVetoIfNegativeEv:
    def test_large_edge_not_vetoed(self):
        model = SlippageModel()
        est = model.estimate("BTC/USDT", qty=0.1, price=100.0, adv_20d=10_000.0)
        assert model.veto_if_negative_ev(expected_edge_bps=50.0, slippage=est) is False

    def test_small_edge_vetoed(self):
        model = SlippageModel()
        est = model.estimate("BTC/USDT", qty=0.1, price=100.0, adv_20d=10_000.0)
        assert model.veto_if_negative_ev(expected_edge_bps=0.5, slippage=est) is True

    def test_exact_breakeven_after_margin_is_vetoed(self):
        # net_edge_bps == 0 is defined as a veto (boundary is exclusive of
        # proceeding — "<=" in the implementation, never let a zero-EV trade
        # through silently).
        model = SlippageModel()
        est = model.estimate("BTC/USDT", qty=1.0, price=100.0, adv_20d=1000.0)
        breakeven_edge = est.total_slippage_bps + model._cfg.slippage_veto_margin_bps
        assert model.veto_if_negative_ev(expected_edge_bps=breakeven_edge, slippage=est)

    def test_just_above_breakeven_not_vetoed(self):
        model = SlippageModel()
        est = model.estimate("BTC/USDT", qty=1.0, price=100.0, adv_20d=1000.0)
        breakeven_edge = est.total_slippage_bps + model._cfg.slippage_veto_margin_bps
        assert not model.veto_if_negative_ev(expected_edge_bps=breakeven_edge + 0.01, slippage=est)

    def test_nan_expected_edge_is_vetoed(self):
        model = SlippageModel()
        est = model.estimate("BTC/USDT", qty=1.0, price=100.0, adv_20d=1000.0)
        assert model.veto_if_negative_ev(expected_edge_bps=math.nan, slippage=est) is True

    def test_zero_margin_config_allows_exact_cost_recovery(self):
        from src.config import RiskSettings

        cfg = RiskSettings(slippage_veto_margin_bps=0.0)
        model = SlippageModel(cfg=cfg)
        est = model.estimate("BTC/USDT", qty=1.0, price=100.0, adv_20d=1000.0)
        # edge exactly equal to slippage with zero margin -> net == 0 -> vetoed
        assert model.veto_if_negative_ev(expected_edge_bps=est.total_slippage_bps, slippage=est)
        # one bps above -> not vetoed
        assert not model.veto_if_negative_ev(
            expected_edge_bps=est.total_slippage_bps + 1.0, slippage=est
        )
