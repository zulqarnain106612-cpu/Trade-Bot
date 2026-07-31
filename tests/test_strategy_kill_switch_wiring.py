"""
Tests for the strategy kill-switch wiring.

StrategyKillSwitchManager was fully implemented but unreachable: nothing
in src/ ever called register_strategy(), and the allocation endpoint
passed every registered strategy as enabled unconditionally, so a
strategy the kill switch had disabled for drift still showed a full
capital allocation.
"""

from __future__ import annotations

import structlog

from src.engine.orchestrator import Orchestrator
from src.risk.performance_drift import PerformanceBaseline
from src.risk.strategy_kill_switch import StrategyKillSwitchManager
from src.strategies.registry import StrategyRegistry
from src.strategies.signal_engine_adapter import SignalEngineStrategy


def _baseline() -> PerformanceBaseline:
    return PerformanceBaseline(
        train_sharpe=2.0,
        oos_sharpe=1.5,
        train_accuracy=0.60,
        oos_accuracy=0.58,
        train_win_rate=0.55,
        max_drawdown_pct=0.10,
        trades_in_backtest=400,
    )


def _orchestrator() -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch._log = structlog.get_logger().bind(component="orchestrator_test")
    return orch


# ---------------------------------------------------------------------------
# enabled_ids — the gate the allocator consumes
# ---------------------------------------------------------------------------


def test_unregistered_strategy_counts_as_enabled():
    # No kill switch means no baseline was ever measured. Treating
    # "unmeasured" as "disabled" would zero the portfolio before startup
    # finishes registering.
    manager = StrategyKillSwitchManager()
    assert manager.enabled_ids(["signal_engine_v1"]) == {"signal_engine_v1"}


def test_registered_and_undrifted_strategy_is_enabled():
    manager = StrategyKillSwitchManager()
    manager.register_strategy("signal_engine_v1", _baseline())
    assert manager.enabled_ids(["signal_engine_v1"]) == {"signal_engine_v1"}


def test_disabled_strategy_is_excluded():
    manager = StrategyKillSwitchManager()
    manager.register_strategy("signal_engine_v1", _baseline())
    manager._states["signal_engine_v1"].enabled = False
    assert manager.enabled_ids(["signal_engine_v1"]) == set()


def test_only_the_disabled_strategy_is_excluded():
    manager = StrategyKillSwitchManager()
    for sid in ("signal_engine_v1", "breakout_volume_v1"):
        manager.register_strategy(sid, _baseline())
    manager._states["breakout_volume_v1"].enabled = False
    assert manager.enabled_ids(["signal_engine_v1", "breakout_volume_v1", "unregistered_v1"]) == {
        "signal_engine_v1",
        "unregistered_v1",
    }


def test_enabled_ids_of_nothing_is_empty():
    assert StrategyKillSwitchManager().enabled_ids([]) == set()


# ---------------------------------------------------------------------------
# is_registered
# ---------------------------------------------------------------------------


def test_is_registered_never_raises_for_unknown_id():
    assert StrategyKillSwitchManager().is_registered("nope") is False


def test_is_registered_true_after_registration():
    manager = StrategyKillSwitchManager()
    manager.register_strategy("signal_engine_v1", _baseline())
    assert manager.is_registered("signal_engine_v1") is True


# ---------------------------------------------------------------------------
# Orchestrator._register_kill_switches
# ---------------------------------------------------------------------------


def test_registers_a_switch_for_every_registered_strategy(monkeypatch):
    import src.engine.orchestrator as orch_mod

    registry = StrategyRegistry()
    registry.register(SignalEngineStrategy())
    manager = StrategyKillSwitchManager()
    monkeypatch.setattr(orch_mod, "get_default_registry", lambda: registry)
    monkeypatch.setattr(orch_mod, "get_strategy_kill_switch_manager", lambda: manager)

    _orchestrator()._register_kill_switches(_baseline())

    assert manager.is_registered("signal_engine_v1")


def test_is_idempotent_so_startup_can_run_twice(monkeypatch):
    # Re-registering raises inside the manager — a kill switch must not
    # silently reset its accumulated drift evidence.
    import src.engine.orchestrator as orch_mod

    registry = StrategyRegistry()
    registry.register(SignalEngineStrategy())
    manager = StrategyKillSwitchManager()
    monkeypatch.setattr(orch_mod, "get_default_registry", lambda: registry)
    monkeypatch.setattr(orch_mod, "get_strategy_kill_switch_manager", lambda: manager)

    orch = _orchestrator()
    orch._register_kill_switches(_baseline())
    manager._states["signal_engine_v1"].enabled = False
    orch._register_kill_switches(_baseline())

    # Second pass must not have reset the disabled state.
    assert manager.is_enabled("signal_engine_v1") is False


def test_empty_registry_registers_nothing(monkeypatch):
    import src.engine.orchestrator as orch_mod

    manager = StrategyKillSwitchManager()
    monkeypatch.setattr(orch_mod, "get_default_registry", StrategyRegistry)
    monkeypatch.setattr(orch_mod, "get_strategy_kill_switch_manager", lambda: manager)

    _orchestrator()._register_kill_switches(_baseline())

    assert manager.enabled_ids(["anything"]) == {"anything"}


def test_registration_failure_does_not_abort_startup(monkeypatch):
    import src.engine.orchestrator as orch_mod

    registry = StrategyRegistry()
    registry.register(SignalEngineStrategy())

    class _Exploding(StrategyKillSwitchManager):
        def register_strategy(self, strategy_id: str, baseline: object) -> None:  # type: ignore[override]
            raise RuntimeError("boom")

    monkeypatch.setattr(orch_mod, "get_default_registry", lambda: registry)
    monkeypatch.setattr(orch_mod, "get_strategy_kill_switch_manager", _Exploding)

    # Startup must survive: a kill-switch registration fault is not a reason
    # to refuse to trade at all.
    _orchestrator()._register_kill_switches(_baseline())
