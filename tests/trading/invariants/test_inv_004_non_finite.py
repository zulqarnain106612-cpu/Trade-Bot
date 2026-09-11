"""
INV-004 — NaN or Infinity cannot produce an executable order.

IEEE-754 makes every comparison against NaN False, so an unguarded NaN does
not trip a threshold -- it walks past it. That is why this is an invariant and
not a footnote: the failure mode is not "the gate rejects the trade with a
confusing message", it is "the gate approves a position of unknown size".

The suite walks the whole path a number takes:

  1. the sizing layer, which must refuse rather than emit a non-finite
     notional;
  2. each gate that compares a measurement against a limit;
  3. the assembled stack, where a non-finite value in any *measured* field
     must not yield a passing result.

Two documented exceptions are asserted rather than assumed, because both look
like holes until you read why they are not:

  - `slippage_estimate=None` means no estimate was produced, and the veto
    fails open by design. A non-finite estimate is the opposite -- an estimate
    that arrived broken -- and blocks.
  - a non-finite whale ratio reduces size instead of halting, because that
    gate is advisory. It still must not read as neutral.
"""

from __future__ import annotations

import math

import pytest

from src.risk.gates import (
    GateStatus,
    check_daily_drawdown,
    check_exchange_stress,
    check_position_exit,
    check_position_size,
    check_slippage_veto,
    check_whale_activity,
    evaluate_all_gates,
)
from src.risk.slippage import SlippageEstimate
from src.strategies.position_sizing import (
    afml_bet_size,
    carver_forecast_position,
    correlation_adjusted_notional,
    estimate_daily_vol,
    recommend_position_notional,
    thorp_kelly_with_variance,
    vol_target_quantity,
)

NON_FINITE = [math.nan, math.inf, -math.inf]
NON_FINITE_IDS = ["nan", "inf", "-inf"]


def _slippage(total_bps: float) -> SlippageEstimate:
    return SlippageEstimate(
        symbol="BTC/USDT",
        qty=0.02,
        notional_usd=1_000.0,
        adv_20d=2.0,
        spread_bps=1.0,
        impact_bps=total_bps - 1.0,
        total_slippage_bps=total_bps,
        total_cost_usd=total_bps / 10_000.0 * 1_000.0,
        participation_rate=0.01,
    )


# ---------------------------------------------------------------------------
# 1. The sizing layer
# ---------------------------------------------------------------------------


class TestSizersRefuseNonFiniteInput:
    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    @pytest.mark.parametrize(
        "field", ["capital_usd", "forecast", "daily_vol_pct", "price", "forecast_scalar"]
    )
    def test_carver_refuses(self, field, bad):
        kwargs = {
            "capital_usd": 100_000.0,
            "forecast": 10.0,
            "daily_vol_pct": 0.02,
            "price": 50_000.0,
        }
        kwargs[field] = bad
        assert carver_forecast_position(**kwargs) == 0.0

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    @pytest.mark.parametrize("field", ["capital_usd", "price", "daily_vol_pct"])
    def test_vol_target_refuses(self, field, bad):
        kwargs = {"capital_usd": 100_000.0, "price": 50_000.0, "daily_vol_pct": 0.02}
        kwargs[field] = bad
        assert vol_target_quantity(**kwargs) == 0.0

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    @pytest.mark.parametrize("field", ["p_long", "capital_usd", "max_fraction"])
    def test_afml_refuses(self, field, bad):
        # The one that actually leaked: `min(nan, max_fraction)` is nan, so
        # the result was `capital_usd * nan`.
        kwargs = {"p_long": 0.7, "capital_usd": 100_000.0, "max_fraction": 0.25}
        kwargs[field] = bad
        result = afml_bet_size(**kwargs)
        assert result == 0.0
        assert math.isfinite(result)

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    @pytest.mark.parametrize(
        "field",
        [
            "win_prob",
            "win_loss_ratio",
            "capital_usd",
            "price",
            "kelly_multiplier",
            "kelly_ceiling",
            "variance_penalty",
        ],
    )
    def test_thorp_refuses(self, field, bad):
        kwargs = {
            "win_prob": 0.6,
            "win_loss_ratio": 1.5,
            "capital_usd": 100_000.0,
            "price": 50_000.0,
        }
        kwargs[field] = bad
        assert thorp_kelly_with_variance(**kwargs) == 0.0

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    def test_correlation_haircut_refuses_an_unreadable_correlation(self, bad):
        # An unreadable correlation is not "uncorrelated": `nan <= threshold`
        # is False, so the reduction used to be `notional * nan`.
        assert correlation_adjusted_notional(1_000.0, bad) == 0.0

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    def test_correlation_haircut_refuses_an_unreadable_notional(self, bad):
        assert correlation_adjusted_notional(bad, 0.1) == 0.0

    def test_a_threshold_of_one_cannot_divide_by_zero(self):
        assert correlation_adjusted_notional(1_000.0, 1.5, threshold=1.0) == 0.0

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    def test_daily_vol_estimate_falls_back_on_a_poisoned_series(self, bad):
        series = [100.0, 101.0, bad, 102.0, 103.0]
        vol = estimate_daily_vol(series)
        assert math.isfinite(vol)
        assert vol == pytest.approx(0.01)

    def test_daily_vol_estimate_falls_back_on_a_non_positive_close(self):
        assert estimate_daily_vol([100.0, 0.0, 101.0]) == pytest.approx(0.01)

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    @pytest.mark.parametrize(
        "field",
        [
            "capital_usd",
            "price",
            "p_long",
            "win_prob",
            "win_loss_ratio",
            "forecast",
            "daily_vol_pct",
            "avg_book_correlation",
            "kelly_multiplier",
            "kelly_ceiling",
        ],
    )
    def test_the_combined_recommendation_refuses(self, field, bad):
        # Before the guard, a NaN here produced a live $10 recommendation:
        # `nan <= 0.0` is False, so the dust floor applied and
        # `max(10.0, nan)` is 10.0.
        kwargs = {
            "capital_usd": 100_000.0,
            "price": 50_000.0,
            "p_long": 0.7,
            "win_prob": 0.6,
            "win_loss_ratio": 1.5,
            "forecast": 10.0,
            "daily_vol_pct": 0.02,
        }
        kwargs[field] = bad
        sized = recommend_position_notional(**kwargs)
        assert sized["recommended"] == 0.0
        assert all(math.isfinite(v) for v in sized.values())


# ---------------------------------------------------------------------------
# 2. Individual gates
# ---------------------------------------------------------------------------


class TestGatesBlockOnNonFiniteMeasurements:
    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    @pytest.mark.parametrize("field", ["daily_pnl_usd", "starting_equity_usd"])
    def test_daily_drawdown_blocks(self, risk_cfg, field, bad):
        kwargs = {"daily_pnl_usd": 0.0, "starting_equity_usd": 100_000.0}
        kwargs[field] = bad
        result = check_daily_drawdown(**kwargs, cfg=risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_DRAWDOWN

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    @pytest.mark.parametrize("field", ["notional_usd", "capital_usd"])
    def test_position_size_blocks(self, risk_cfg, field, bad):
        kwargs = {"notional_usd": 1_000.0, "capital_usd": 100_000.0}
        kwargs[field] = bad
        result = check_position_size(**kwargs, cfg=risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_POSITION_SIZE

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    def test_slippage_veto_blocks_on_a_broken_edge(self, risk_cfg, bad):
        result = check_slippage_veto(bad, _slippage(5.0), risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_NEGATIVE_EV

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    def test_slippage_veto_blocks_on_a_broken_estimate(self, risk_cfg, bad):
        result = check_slippage_veto(100.0, _slippage(bad), risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_NEGATIVE_EV

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    def test_a_broken_edge_with_no_estimate_still_fails_open(self, risk_cfg, bad):
        # The documented exception: None means "nothing was computed", and the
        # veto is additive. Asserted so that changing it is a visible decision.
        assert check_slippage_veto(bad, None, risk_cfg).passed

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    def test_exchange_stress_blocks(self, bad):
        result = check_exchange_stress(bad)
        assert not result.passed
        assert result.status is GateStatus.HALT_EXCHANGE_STRESS

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    def test_whale_ratio_reduces_rather_than_reading_as_neutral(self, bad):
        # Advisory by design: in advisory mode it shrinks the position; with
        # advisory off it vetoes. Either way it must not pass as neutral.
        advisory = check_whale_activity(bad, advisory=True, advisory_scalar=0.5)
        assert advisory.passed
        assert advisory.size_scalar == 0.5
        assert advisory.status is GateStatus.REDUCE_WHALE_ACTIVITY

        blocking = check_whale_activity(bad, advisory=False)
        assert not blocking.passed

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    def test_an_unreadable_mark_closes_the_position(self, bad):
        # Not a gate on entry, but the same class of defect on exit: all three
        # exit comparisons are False for NaN, so the position would sit
        # unprotected until the time exit.
        assert (
            check_position_exit(
                unrealized_pnl_pct=bad,
                entry_ts_ms=0,
                now_ts_ms=1_000,
                stop_loss_enabled=True,
                stop_loss_pct=2.0,
                take_profit_enabled=True,
                take_profit_pct=4.0,
                max_holding_period_s=86_400.0,
            )
            == "invalid_mark"
        )


# ---------------------------------------------------------------------------
# 3. The assembled stack
# ---------------------------------------------------------------------------


class TestTheStackNeverApprovesAnUnmeasurableTrade:
    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    @pytest.mark.parametrize(
        ("field", "status"),
        [
            ("daily_pnl_usd", GateStatus.HALT_DRAWDOWN),
            ("starting_equity_usd", GateStatus.HALT_DRAWDOWN),
            ("notional_usd", GateStatus.HALT_POSITION_SIZE),
            ("capital_usd", GateStatus.HALT_POSITION_SIZE),
            ("exchange_stress_score", GateStatus.HALT_EXCHANGE_STRESS),
        ],
    )
    def test_a_non_finite_measurement_halts(self, passing_gate_ctx, risk_cfg, field, status, bad):
        ctx = passing_gate_ctx(**{field: bad})
        result = evaluate_all_gates(ctx, risk_cfg)
        assert not result.passed
        assert result.status is status

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    def test_a_non_finite_edge_halts_once_an_estimate_exists(self, passing_gate_ctx, risk_cfg, bad):
        ctx = passing_gate_ctx(expected_edge_bps=bad, slippage_estimate=_slippage(5.0))
        result = evaluate_all_gates(ctx, risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_NEGATIVE_EV

    @pytest.mark.parametrize("bad", NON_FINITE, ids=NON_FINITE_IDS)
    def test_a_non_finite_whale_ratio_never_yields_full_size(self, passing_gate_ctx, risk_cfg, bad):
        ctx = passing_gate_ctx(whale_buy_sell_ratio=bad)
        result = evaluate_all_gates(ctx, risk_cfg)
        # Default posture is veto; the advisory posture reduces. Neither is
        # "proceed at full size", which is the thing the invariant forbids.
        assert (not result.passed) or result.size_scalar < 1.0
