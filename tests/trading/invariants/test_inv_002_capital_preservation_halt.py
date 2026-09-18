"""
INV-002 — No new entry while a capital-preservation halt is active.

The floor is the outermost control in the stack: it is evaluated first, it
does not clear on equity recovery, and nothing automated may lift it. Each of
those three properties is a separate way the invariant could be lost, so each
gets its own assertion here rather than being folded into one "it halts" test.

The fourth way it could be lost is ordering: if any gate ran before the floor
and returned a *pass*, the stack would still be correct, but if one ran before
it and short-circuited on some other condition the audit record would name the
wrong control. The evaluation-order test pins the floor as gate zero.
"""

from __future__ import annotations

import math

import pytest

from src.risk.capital_preservation_floor import CapitalPreservationFloor
from src.risk.gates import (
    GateStatus,
    check_capital_preservation_floor,
    evaluate_all_gates,
)


class TestTheFloorItself:
    def test_it_halts_at_the_threshold(self):
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
        assert floor.update_equity(100_000.0)
        # Exactly 30% down. The comparison is `>=`, so the threshold halts.
        assert not floor.update_equity(70_000.0)
        assert floor.is_halted

    def test_one_dollar_inside_the_threshold_does_not_halt(self):
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
        floor.update_equity(100_000.0)
        assert floor.update_equity(70_001.0)
        assert not floor.is_halted

    def test_recovery_does_not_clear_it(self):
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
        floor.update_equity(100_000.0)
        floor.update_equity(60_000.0)
        assert floor.is_halted
        # Back above the old peak, and still halted. This is the whole point
        # of the floor versus the daily drawdown gate.
        for equity in (80_000.0, 100_000.0, 250_000.0):
            assert not floor.update_equity(equity)
            assert floor.is_halted

    def test_only_re_authorization_clears_it(self):
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
        floor.update_equity(100_000.0)
        floor.update_equity(50_000.0)
        assert floor.is_halted

        floor.re_authorize(authorized_by="operator", reason="reviewed", at_ms=1)
        assert not floor.is_halted
        assert floor.last_reauthorization.authorized_by == "operator"

    def test_re_authorization_requires_an_author(self):
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
        with pytest.raises(ValueError, match="authorized_by"):
            floor.re_authorize(authorized_by="", reason="whoops", at_ms=1)

    def test_re_authorization_does_not_reset_the_peak(self):
        # A cleared halt that also forgot the peak would re-arm at the *new*,
        # lower equity, so a second 30% fall would have to be much larger in
        # dollars before it fired again.
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
        floor.update_equity(100_000.0)
        floor.update_equity(65_000.0)
        floor.re_authorize(authorized_by="operator", reason="reviewed", at_ms=1)
        # Still measured against the 100k peak: 69k is a 31% drawdown.
        assert not floor.update_equity(69_000.0)
        assert floor.is_halted

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_a_non_finite_mark_raises_rather_than_disabling_the_floor(self, bad):
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
        floor.update_equity(100_000.0)
        with pytest.raises(ValueError):
            floor.update_equity(bad)
        # And the floor still works afterwards -- the bad mark never reached
        # _peak_equity, so it did not poison every later comparison.
        assert not floor.update_equity(50_000.0)
        assert floor.is_halted

    def test_a_negative_mark_raises(self):
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
        with pytest.raises(ValueError, match="non-negative"):
            floor.update_equity(-1.0)

    @pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5])
    def test_an_impossible_threshold_is_refused_at_construction(self, bad):
        with pytest.raises(ValueError, match="max_drawdown_pct"):
            CapitalPreservationFloor(max_drawdown_pct=bad)


class TestTheGateReadsTheFloor:
    def test_halted_blocks(self):
        result = check_capital_preservation_floor(halted=True)
        assert not result.passed
        assert result.status is GateStatus.HALT_CAPITAL_PRESERVATION

    def test_not_halted_passes(self):
        assert check_capital_preservation_floor(halted=False).passed


class TestTheStackCannotBeTalkedPast:
    @pytest.mark.parametrize(
        "overrides",
        [
            {},
            {"notional_usd": 0.0},
            {"expected_edge_bps": 1e6},
            {"regime_state": 0},
            {"consecutive_loss_count": 0, "daily_pnl_usd": 5_000.0},
            {"exchange_stress_score": 0.0, "whale_buy_sell_ratio": 5.0},
        ],
        ids=["plain", "flat", "huge-edge", "calm-regime", "profitable-day", "calm-intel"],
    )
    def test_a_halt_blocks_every_otherwise_perfect_entry(
        self, passing_gate_ctx, risk_cfg, overrides
    ):
        ctx = passing_gate_ctx(capital_preservation_halted=True, **overrides)
        result = evaluate_all_gates(ctx, risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_CAPITAL_PRESERVATION

    def test_the_floor_is_evaluated_first(self, passing_gate_ctx, risk_cfg):
        # Break the position-size gate as well. If the floor were not first,
        # the reported status would be HALT_POSITION_SIZE and the audit trail
        # would blame the wrong control for the halt.
        ctx = passing_gate_ctx(
            capital_preservation_halted=True,
            notional_usd=1e9,
            daily_pnl_usd=-1e9,
            consecutive_loss_count=99,
            regime_state=2,
        )
        assert evaluate_all_gates(ctx, risk_cfg).status is GateStatus.HALT_CAPITAL_PRESERVATION

    def test_end_to_end_from_the_floor_object(self, passing_gate_ctx, risk_cfg):
        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
        floor.update_equity(100_000.0)
        floor.update_equity(50_000.0)

        ctx = passing_gate_ctx(capital_preservation_halted=floor.is_halted)
        assert not evaluate_all_gates(ctx, risk_cfg).passed

        floor.re_authorize(authorized_by="operator", reason="reviewed", at_ms=1)
        ctx = passing_gate_ctx(capital_preservation_halted=floor.is_halted)
        assert evaluate_all_gates(ctx, risk_cfg).passed
