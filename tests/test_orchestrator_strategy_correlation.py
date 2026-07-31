"""
Tests for the strategy-correlation feed and sizing scalar.

StrategyCorrelationTracker had no producer — nothing in src/ ever called
push_strategy_returns — so it was permanently empty and every scalar it
returned was a no-op 1.0. Two strategies can be uncorrelated as assets
while running the same underlying bet, so this is an independent ceiling
on position size, not a duplicate of the asset-level one.
"""

from __future__ import annotations

import structlog

from src.engine.orchestrator import Orchestrator
from src.strategies.signal_engine_adapter import STRATEGY_ID_SIGNAL_ENGINE


def _orchestrator() -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch._log = structlog.get_logger().bind(component="orchestrator_test")
    orch._last_unrealized_by_trade = {}
    return orch


def _pos(
    trade_id: str,
    strategy_id: str = STRATEGY_ID_SIGNAL_ENGINE,
    unrealized_pnl: float = 0.0,
    notional_usd: float = 1_000.0,
) -> dict[str, object]:
    return {
        "trade_id": trade_id,
        "strategy_id": strategy_id,
        "unrealized_pnl": unrealized_pnl,
        "notional_usd": notional_usd,
    }


# ---------------------------------------------------------------------------
# _push_strategy_returns
# ---------------------------------------------------------------------------


def test_first_snapshot_pushes_nothing(monkeypatch):
    # No prior mark to difference against — a strategy's first appearance
    # is not a return.
    import src.engine.orchestrator as orch_mod

    pushed: list[dict[str, float]] = []
    monkeypatch.setattr(
        orch_mod,
        "get_strategy_correlation",
        lambda: type("T", (), {"push_strategy_returns": staticmethod(pushed.append)})(),
    )

    _orchestrator()._push_strategy_returns([_pos("t1", unrealized_pnl=10.0)])

    assert pushed == []


def test_second_snapshot_pushes_the_mark_to_market_delta(monkeypatch):
    import src.engine.orchestrator as orch_mod

    pushed: list[dict[str, float]] = []
    monkeypatch.setattr(
        orch_mod,
        "get_strategy_correlation",
        lambda: type("T", (), {"push_strategy_returns": staticmethod(pushed.append)})(),
    )

    orch = _orchestrator()
    orch._push_strategy_returns([_pos("t1", unrealized_pnl=10.0, notional_usd=1_000.0)])
    orch._push_strategy_returns([_pos("t1", unrealized_pnl=30.0, notional_usd=1_000.0)])

    # (30 - 10) / 1000
    assert pushed == [{STRATEGY_ID_SIGNAL_ENGINE: 0.02}]


def test_positions_of_one_strategy_are_aggregated(monkeypatch):
    import src.engine.orchestrator as orch_mod

    pushed: list[dict[str, float]] = []
    monkeypatch.setattr(
        orch_mod,
        "get_strategy_correlation",
        lambda: type("T", (), {"push_strategy_returns": staticmethod(pushed.append)})(),
    )

    orch = _orchestrator()
    first = [
        _pos("t1", unrealized_pnl=0.0, notional_usd=1_000.0),
        _pos("t2", unrealized_pnl=0.0, notional_usd=3_000.0),
    ]
    second = [
        _pos("t1", unrealized_pnl=10.0, notional_usd=1_000.0),
        _pos("t2", unrealized_pnl=30.0, notional_usd=3_000.0),
    ]
    orch._push_strategy_returns(first)
    orch._push_strategy_returns(second)

    # (10 + 30) / (1000 + 3000)
    assert pushed == [{STRATEGY_ID_SIGNAL_ENGINE: 0.01}]


def test_separate_strategies_get_separate_returns(monkeypatch):
    import src.engine.orchestrator as orch_mod

    pushed: list[dict[str, float]] = []
    monkeypatch.setattr(
        orch_mod,
        "get_strategy_correlation",
        lambda: type("T", (), {"push_strategy_returns": staticmethod(pushed.append)})(),
    )

    orch = _orchestrator()
    orch._push_strategy_returns(
        [_pos("t1"), _pos("t2", strategy_id="breakout_volume_v1")],
    )
    orch._push_strategy_returns(
        [
            _pos("t1", unrealized_pnl=50.0),
            _pos("t2", strategy_id="breakout_volume_v1", unrealized_pnl=-20.0),
        ],
    )

    assert pushed == [{STRATEGY_ID_SIGNAL_ENGINE: 0.05, "breakout_volume_v1": -0.02}]


def test_closed_position_marks_are_dropped(monkeypatch):
    # A stale mark must not resurface as a spurious delta if the trade_id
    # is ever reused.
    import src.engine.orchestrator as orch_mod

    pushed: list[dict[str, float]] = []
    monkeypatch.setattr(
        orch_mod,
        "get_strategy_correlation",
        lambda: type("T", (), {"push_strategy_returns": staticmethod(pushed.append)})(),
    )

    orch = _orchestrator()
    orch._push_strategy_returns([_pos("t1", unrealized_pnl=10.0)])
    orch._push_strategy_returns([])  # t1 closed
    assert orch._last_unrealized_by_trade == {}

    orch._push_strategy_returns([_pos("t1", unrealized_pnl=999.0)])
    assert pushed == []  # treated as a fresh position, not a 989 delta


def test_zero_notional_position_is_skipped(monkeypatch):
    import src.engine.orchestrator as orch_mod

    pushed: list[dict[str, float]] = []
    monkeypatch.setattr(
        orch_mod,
        "get_strategy_correlation",
        lambda: type("T", (), {"push_strategy_returns": staticmethod(pushed.append)})(),
    )

    orch = _orchestrator()
    orch._push_strategy_returns([_pos("t1", unrealized_pnl=0.0, notional_usd=0.0)])
    orch._push_strategy_returns([_pos("t1", unrealized_pnl=10.0, notional_usd=0.0)])

    assert pushed == []


def test_positions_without_strategy_id_are_skipped(monkeypatch):
    import src.engine.orchestrator as orch_mod

    pushed: list[dict[str, float]] = []
    monkeypatch.setattr(
        orch_mod,
        "get_strategy_correlation",
        lambda: type("T", (), {"push_strategy_returns": staticmethod(pushed.append)})(),
    )

    orch = _orchestrator()
    bare = {"trade_id": "t1", "unrealized_pnl": 10.0, "notional_usd": 1_000.0}
    orch._push_strategy_returns([bare])
    orch._push_strategy_returns([bare])

    assert pushed == []


def test_tracker_failure_does_not_break_the_monitor_loop(monkeypatch):
    import src.engine.orchestrator as orch_mod

    def _boom():
        raise RuntimeError("tracker exploded")

    monkeypatch.setattr(orch_mod, "get_strategy_correlation", _boom)

    orch = _orchestrator()
    orch._push_strategy_returns([_pos("t1", unrealized_pnl=0.0)])
    orch._push_strategy_returns([_pos("t1", unrealized_pnl=10.0)])  # must not raise


# ---------------------------------------------------------------------------
# _strategy_correlation_scalar
# ---------------------------------------------------------------------------


def test_scalar_is_one_when_no_other_strategy_holds_capital():
    orch = _orchestrator()
    assert orch._strategy_correlation_scalar([]) == 1.0
    assert orch._strategy_correlation_scalar([_pos("t1")]) == 1.0


def test_scalar_consults_tracker_for_other_active_strategies(monkeypatch):
    import src.engine.orchestrator as orch_mod

    seen: dict[str, object] = {}

    class _Tracker:
        def correlation_scalar(self, new_strategy_id, active_strategy_ids):
            seen["new"] = new_strategy_id
            seen["active"] = active_strategy_ids
            return 0.5

    monkeypatch.setattr(orch_mod, "get_strategy_correlation", _Tracker)

    orch = _orchestrator()
    scalar = orch._strategy_correlation_scalar(
        [
            _pos("t1"),
            _pos("t2", strategy_id="breakout_volume_v1"),
            _pos("t3", strategy_id="breakout_volume_v1"),
        ]
    )

    assert scalar == 0.5
    assert seen["new"] == STRATEGY_ID_SIGNAL_ENGINE
    # Deduplicated — two positions of one strategy are one active strategy.
    assert seen["active"] == ["breakout_volume_v1"]
