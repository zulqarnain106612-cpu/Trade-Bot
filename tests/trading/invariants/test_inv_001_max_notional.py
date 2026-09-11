"""
INV-001 — No order exceeds the configured maximum notional.

The invariant is about the *composition*, not about one function. A sizer that
respects the ceiling is not enough if some other path can reach the executor,
and a gate that enforces the ceiling is not enough if it can be skipped. So
this suite asserts three things:

  1. `check_position_size` decides the ceiling exactly, including at the
     boundary.
  2. No combination of otherwise-passing inputs lets an over-ceiling notional
     through `evaluate_all_gates`.
  3. The sizing layer feeding that gate never produces a recommendation the
     gate would have to catch -- or, where it can (the dust floor on a tiny
     account), the gate does catch it.

Point 3 is the one worth reading. `recommend_position_notional` floors a
positive recommendation up to $10 so it does not suggest a sub-exchange-minimum
trade. On a $150 account that $10 is 6.7% of capital, above the 5% ceiling. The
sizer is not wrong to suggest it and the ceiling is not wrong to refuse it --
but the invariant only holds because the gate is downstream, and a test that
checked the sizer alone would have concluded otherwise.
"""

from __future__ import annotations

import pytest

from src.risk.gates import GateStatus, check_position_size, evaluate_all_gates
from src.strategies.position_sizing import recommend_position_notional

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _pct(notional: float, capital: float) -> float:
    return (notional / capital) * 100.0


class TestTheGateDecidesTheCeiling:
    def test_under_the_ceiling_passes(self, risk_cfg):
        capital = 100_000.0
        notional = capital * (risk_cfg.max_position_size_pct - 1.0) / 100.0
        assert check_position_size(notional, capital, risk_cfg).passed

    def test_exactly_at_the_ceiling_passes(self, risk_cfg):
        # The comparison is `position_pct > max_pct`, so the limit itself is
        # allowed. Pinned here because flipping it to `>=` is a one-character
        # change that silently narrows every position by one tick.
        capital = 100_000.0
        notional = capital * risk_cfg.max_position_size_pct / 100.0
        result = check_position_size(notional, capital, risk_cfg)
        assert result.passed
        assert _pct(notional, capital) == pytest.approx(risk_cfg.max_position_size_pct)

    def test_one_cent_over_the_ceiling_fails(self, risk_cfg):
        capital = 100_000.0
        notional = capital * risk_cfg.max_position_size_pct / 100.0 + 0.01
        result = check_position_size(notional, capital, risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_POSITION_SIZE

    @pytest.mark.parametrize("capital", [1.0, 100.0, 10_000.0, 1_000_000.0, 5e8])
    def test_the_ceiling_scales_with_capital(self, risk_cfg, capital):
        limit = capital * risk_cfg.max_position_size_pct / 100.0
        assert check_position_size(limit, capital, risk_cfg).passed
        assert not check_position_size(limit * 1.001, capital, risk_cfg).passed

    def test_zero_notional_passes(self, risk_cfg):
        # Not a trade, and refusing it would make "flat" an error state.
        assert check_position_size(0.0, 100_000.0, risk_cfg).passed

    def test_non_positive_capital_is_refused_not_divided_by(self, risk_cfg):
        for capital in (0.0, -1.0):
            result = check_position_size(1.0, capital, risk_cfg)
            assert not result.passed
            assert result.status is GateStatus.HALT_POSITION_SIZE


class TestTheStackCannotBeTalkedPast:
    """An over-ceiling notional must be refused whatever else is true."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {},
            {"regime_state": 0},
            {"consecutive_loss_count": 2},
            {"daily_pnl_usd": -100.0},
            {"exchange_stress_score": 0.1},
            {"whale_buy_sell_ratio": 2.0},
            {"expected_edge_bps": 10_000.0},
            {"paper_trading_days": 10_000},
        ],
        ids=[
            "plain",
            "ranging-regime",
            "some-losses",
            "small-loss",
            "calm-exchange",
            "whales-buying",
            "huge-edge",
            "long-paper-history",
        ],
    )
    def test_over_ceiling_is_always_blocked(self, passing_gate_ctx, risk_cfg, overrides):
        capital = 100_000.0
        over = capital * risk_cfg.max_position_size_pct / 100.0 + 1.0
        ctx = passing_gate_ctx(notional_usd=over, capital_usd=capital, **overrides)
        result = evaluate_all_gates(ctx, risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_POSITION_SIZE

    def test_at_the_ceiling_the_stack_passes(self, passing_gate_ctx, risk_cfg):
        capital = 100_000.0
        at = capital * risk_cfg.max_position_size_pct / 100.0
        ctx = passing_gate_ctx(notional_usd=at, capital_usd=capital)
        assert evaluate_all_gates(ctx, risk_cfg).passed


class TestTheSizerIsACeilingAndTheGateIsTheAuthority:
    """
    The relationship between the two controls, stated rather than assumed.

    `recommend_position_notional` is *not* the order size. It is the Carver
    "whichever method gives the smaller position" cap that
    `src/engine/signal_engine.py` applies to Kelly's output before Kelly's
    result reaches the gate. Its own ceiling is 25% of capital -- five times
    the gate's 5% -- so on an ordinary account the sizer routinely proposes a
    cap the gate would refuse outright.

    That is not a bug, and the first draft of this suite asserted it was:
    a cap of 10% does not mean a 10% order, it means "no larger than 10%",
    and Kelly then sizes underneath it. What the invariant requires is
    narrower and stronger -- **nothing the sizing layer produces can
    authorise an order above the gate's ceiling** -- so that is what is
    asserted here.
    """

    @pytest.mark.parametrize("capital", [10_000.0, 250_000.0, 5_000_000.0])
    @pytest.mark.parametrize("p_long", [0.55, 0.70, 0.95])
    def test_the_cap_never_exceeds_the_sizers_own_ceiling(self, capital, p_long):
        sized = recommend_position_notional(
            capital_usd=capital,
            price=50_000.0,
            p_long=p_long,
            win_prob=p_long,
            win_loss_ratio=1.5,
            forecast=8.0,
            daily_vol_pct=0.02,
        )
        recommended = sized["recommended"]
        assert 0.0 <= recommended <= capital * 0.25 + 1e-6

    @pytest.mark.parametrize("capital", [10_000.0, 250_000.0, 5_000_000.0])
    @pytest.mark.parametrize("p_long", [0.55, 0.70, 0.95])
    def test_whatever_the_cap_says_the_gate_still_decides(self, risk_cfg, capital, p_long):
        sized = recommend_position_notional(
            capital_usd=capital,
            price=50_000.0,
            p_long=p_long,
            win_prob=p_long,
            win_loss_ratio=1.5,
            forecast=8.0,
            daily_vol_pct=0.02,
        )
        cap = sized["recommended"]
        limit = capital * risk_cfg.max_position_size_pct / 100.0
        # An order sized under both is fine; an order sized at the cap when
        # the cap is above the gate's limit is refused. Both halves matter --
        # the first says the controls compose, the second says the gate wins.
        assert check_position_size(min(cap, limit), capital, risk_cfg).passed
        if cap > limit:
            assert not check_position_size(cap, capital, risk_cfg).passed

    def test_the_sizers_ceiling_is_wider_than_the_gates(self, risk_cfg):
        # Recorded as a fact about the configuration, not an accident. If the
        # gate is ever widened past 25%, this fails and the composition above
        # has to be re-argued.
        assert risk_cfg.max_position_size_pct < 25.0

    def test_the_dust_floor_can_exceed_the_ceiling_and_the_gate_catches_it(self, risk_cfg):
        # $150 of capital: the 5% ceiling is $7.50, below the $10 dust floor.
        # The sizer is entitled to say "below this it is not worth trading";
        # the invariant holds because the gate refuses the result.
        capital = 150.0
        sized = recommend_position_notional(
            capital_usd=capital,
            price=50_000.0,
            p_long=0.51,
            win_prob=0.51,
            win_loss_ratio=2.0,
            forecast=15.0,
            daily_vol_pct=0.02,
        )
        recommended = sized["recommended"]
        assert recommended == pytest.approx(10.0)
        assert _pct(recommended, capital) > risk_cfg.max_position_size_pct
        assert not check_position_size(recommended, capital, risk_cfg).passed

    def test_no_edge_recommends_nothing(self):
        sized = recommend_position_notional(
            capital_usd=100_000.0,
            price=50_000.0,
            p_long=0.5,
            win_prob=0.5,
            win_loss_ratio=1.0,
            forecast=0.0,
            daily_vol_pct=0.02,
        )
        assert sized["recommended"] == 0.0
