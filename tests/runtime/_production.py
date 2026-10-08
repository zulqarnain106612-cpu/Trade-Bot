"""Builders for the production-runtime tests: the real objects the running
process holds (Orchestrator, SignalEngine, EngineOrchestrator, kill switch,
pause switch, registries), with storage and the market-data fetcher mocked
-- nothing here touches the network or a file outside tmp_path."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest

import src.engine.crypto_box_adapter as crypto_box_adapter
import src.risk.strategy_kill_switch as kill_switch_module
from src.config import Timeframe
from src.engine.orchestrator import Orchestrator
from src.engine.signal_engine import ShadowBundle, SignalEngine
from src.eventbus import EventBus
from src.risk.performance_drift import PerformanceBaseline
from src.risk.strategy_kill_switch import StrategyKillSwitchManager
from src.runtime.production import ProductionSources
from src.strategies.registry import Signal, StrategyRegistry
from src.tuning.registry import ParameterRegistry
from src.tuning.state import _PauseState

TF = Timeframe("15m")
STRATEGY = "trend"

BASELINE = PerformanceBaseline(
    train_sharpe=1.2,
    oos_sharpe=1.0,
    train_accuracy=0.6,
    oos_accuracy=0.55,
    train_win_rate=0.55,
    max_drawdown_pct=0.1,
    trades_in_backtest=200,
)


@dataclass
class _Strategy:
    strategy_id: str

    def generate_signal(self, bar: object) -> Signal:
        return Signal(0, 0.0, 0.0)

    def required_capital_fraction(self) -> float:
        return 0.25


def signal_engine() -> SignalEngine:
    mock = MagicMock
    return SignalEngine("BTC/USDT", TF, mock(), mock(), mock(), mock(), mock(), mock())


async def with_shadow(engine: SignalEngine, model_id: str = "m-2") -> SignalEngine:
    await engine.set_shadow_bundle(ShadowBundle(model_id, MagicMock(), MagicMock(), MagicMock()))
    return engine


def orchestrator(
    monkeypatch: pytest.MonkeyPatch,
    *,
    engines: dict[str, SignalEngine] | None = None,
    crypto_box: bool = False,
) -> Orchestrator:
    """A real Orchestrator, constructed but not started (no storage, no I/O)."""
    if crypto_box:
        monkeypatch.setattr(crypto_box_adapter, "_ENABLED", True)
    orch = Orchestrator(MagicMock(), MagicMock())
    if crypto_box:  # the adapter built the real 18-engine orchestrator itself
        assert orch.crypto_box.engine_orchestrator is not None
    orch._engines = dict(engines or {})
    return orch


@dataclass
class World:
    """One process's worth of runtime objects, isolated from the singletons."""

    bus: EventBus = field(default_factory=EventBus)
    strategies: StrategyRegistry = field(default_factory=StrategyRegistry)
    kill_switch: StrategyKillSwitchManager = field(default_factory=StrategyKillSwitchManager)
    parameters: ParameterRegistry = field(default_factory=ParameterRegistry)
    pause: _PauseState = field(default_factory=_PauseState)
    orch: Orchestrator | None = None
    scheduler_running: bool = False
    api_tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    pause_audit: list[bool] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def sources(self) -> ProductionSources:
        return ProductionSources(
            bus=self.bus,
            strategies=self.strategies,
            kill_switch=self.kill_switch,
            parameters=self.parameters,
            pause=self.pause,
            on_pause_change=self.pause_audit.append,
            orchestrator=lambda: self.orch,
            scheduler_running=lambda: self.scheduler_running,
            api_tasks=lambda: tuple(self.api_tasks),
            clock_ms=lambda: 1_700_000_000_000,
        )


def world(
    monkeypatch: pytest.MonkeyPatch,
    *,
    strategies: Iterable[str] = (STRATEGY,),
    switched: Iterable[str] = (STRATEGY,),
) -> World:
    """Strategies registered, kill switches on ``switched``; decision-log
    writes captured instead of written to the configured path."""
    w = World()
    monkeypatch.setattr(
        kill_switch_module,
        "_record_structural_change",
        lambda **kw: w.decisions.append(kw),
    )
    for sid in strategies:
        w.strategies.register(_Strategy(sid))
    for sid in switched:
        w.kill_switch.register_strategy(sid, BASELINE)
    return w
