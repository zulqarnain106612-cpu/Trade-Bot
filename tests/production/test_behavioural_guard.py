"""
REL-007 — trading that does not look like us is halted.

Every other control checks whether an action is permitted. This one checks
whether the *pattern* is the pattern this bot produces, and the gap between
the two is where the expensive failures live: stolen credentials used
correctly, a model that has quietly started doing something else, a retry
loop that has begun resubmitting.

Each of those produces perfectly legal orders. Every one passes the risk
gates. What gives them away is shape — forty orders in a minute from a bot
that places four an hour, a symbol never traded before, a 3am burst from a
London-session strategy.

The tests are those three incidents plus the boundary cases that decide
whether the guard is usable: a normal session must not trip it, or somebody
will switch it off within a week.
"""

from __future__ import annotations

import pytest

from src.diagnostics.behavioural_guard import (
    OrderEvent,
    Profile,
    evaluate,
)

PROFILE = Profile(
    allowed_symbols=frozenset({"BTC/USDT", "ETH/USDT"}),
    trading_hours_utc=frozenset(range(7, 21)),
)


def order(ts: float, symbol="BTC/USDT", side="buy", notional=1_000.0, hour=12) -> OrderEvent:
    return OrderEvent(ts=ts, symbol=symbol, side=side, notional_usd=notional, hour_utc=hour)


def normal_session(n: int = 20) -> list[OrderEvent]:
    # One order every ten minutes, alternating direction, even size.
    return [order(i * 600.0, side="buy" if i % 2 else "sell", notional=1_000.0) for i in range(n)]


class TestNormalTradingPasses:
    def test_a_quiet_session_is_within_profile(self):
        assert evaluate(normal_session(), PROFILE).should_halt is False

    def test_an_empty_window_is_not_an_anomaly(self):
        # No orders is the normal state of a bot waiting for a signal.
        assert evaluate([], PROFILE).should_halt is False

    def test_a_single_order_is_not_an_anomaly(self):
        assert evaluate([order(0.0)], PROFILE).should_halt is False

    def test_both_permitted_symbols_are_fine(self):
        events = [order(0.0, symbol="BTC/USDT"), order(600.0, symbol="ETH/USDT")]
        assert evaluate(events, PROFILE).should_halt is False


class TestTheStolenCredential:
    def test_an_unknown_symbol_halts(self):
        # The clearest single signal: the key is valid, the order is legal,
        # and the symbol is one this bot has never traded.
        events = [*normal_session(5), order(3_000.0, symbol="DOGE/USDT")]
        verdict = evaluate(events, PROFILE)
        assert verdict.should_halt
        assert any(a.kind == "unknown_symbol" for a in verdict.anomalies)

    def test_off_hours_activity_halts(self):
        events = [*normal_session(5), order(3_000.0, hour=3)]
        verdict = evaluate(events, PROFILE)
        assert any(a.kind == "off_hours" for a in verdict.anomalies)

    def test_an_outsized_order_halts(self):
        events = [*normal_session(10), order(6_000.0, notional=500_000.0)]
        verdict = evaluate(events, PROFILE)
        assert any(a.kind == "outsized_order" for a in verdict.anomalies)

    def test_the_combination_is_reported_in_full(self):
        # During an incident the combination is the diagnosis: an unknown
        # symbol *and* a burst is a different event from either alone.
        events = [order(i * 0.6, symbol="DOGE/USDT", hour=3) for i in range(30)]
        verdict = evaluate(events, PROFILE)
        kinds = {a.kind for a in verdict.anomalies}
        assert {"unknown_symbol", "off_hours"} <= kinds


class TestTheRunawayLoop:
    def test_a_burst_halts(self):
        events = [order(i * 1.0) for i in range(30)]  # 30 in 30 seconds
        verdict = evaluate(events, PROFILE)
        assert any(a.kind == "order_burst" for a in verdict.anomalies)

    def test_orders_milliseconds_apart_halt(self):
        # Not a strategy decision: a retry loop or a duplicate submission.
        events = [order(0.0), order(0.05)]
        verdict = evaluate(events, PROFILE)
        assert any(a.kind == "order_interval" for a in verdict.anomalies)

    def test_a_sustained_rate_halts_even_when_no_minute_bursts(self):
        # Below the per-minute bound throughout, over the per-hour bound in
        # aggregate: the slow flood a per-minute check alone would miss.
        events = [order(i * 30.0) for i in range(80)]
        verdict = evaluate(events, PROFILE)
        assert any(a.kind == "sustained_rate" for a in verdict.anomalies)


class TestTheModelThatChanged:
    def test_a_long_one_way_run_halts(self):
        # A model that has stopped disagreeing with itself, or one input
        # stuck at a constant.
        events = [order(i * 600.0, side="buy") for i in range(20)]
        verdict = evaluate(events, PROFILE)
        assert any(a.kind == "directional_run" for a in verdict.anomalies)

    def test_a_short_run_is_normal(self):
        events = [order(i * 600.0, side="buy") for i in range(5)]
        assert evaluate(events, PROFILE).should_halt is False


class TestTheProfileIsDeclaredNotLearned:
    def test_the_profile_is_explicit_data(self):
        # A learned baseline adapts to an attacker who moves slowly, and makes
        # the first hour of a compromise the new normal.
        assert PROFILE.allowed_symbols == frozenset({"BTC/USDT", "ETH/USDT"})
        assert 3 not in PROFILE.trading_hours_utc

    def test_a_wider_profile_permits_more(self):
        # Changing what is normal is a diff, which is the point.
        wide = Profile(
            allowed_symbols=frozenset({"BTC/USDT", "DOGE/USDT"}),
            trading_hours_utc=frozenset(range(24)),
        )
        events = [order(0.0, symbol="DOGE/USDT", hour=3)]
        assert evaluate(events, PROFILE).should_halt is True
        assert evaluate(events, wide).should_halt is False

    @pytest.mark.parametrize(
        "field,value",
        [
            ("max_orders_per_minute", 10),
            ("max_orders_per_hour", 60),
            ("max_consecutive_same_direction", 12),
        ],
    )
    def test_the_defaults_are_the_declared_ones(self, field, value):
        assert getattr(PROFILE, field) == value

    def test_the_verdict_explains_itself(self):
        verdict = evaluate([order(0.0, symbol="DOGE/USDT")], PROFILE)
        assert "DOGE/USDT" in verdict.report()

    def test_a_clean_verdict_says_so(self):
        assert evaluate(normal_session(), PROFILE).report() == "behaviour within profile"
