"""GOV-067: adapters read the existing registries -- which keep working
unchanged -- into runtime specs, declaring only actions the subsystem has."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from src.engines.orchestrator import EngineOrchestrator
from src.models.model_registry import ModelRegistry
from src.runtime.adapters import (
    SHADOW_MODEL_CAPABILITIES,
    UNVERSIONED,
    engine_discoveries,
    eventbus_discovery,
    model_discoveries,
    provider_discoveries,
    safe_name,
    strategy_discoveries,
    task_discoveries,
    tuning_discoveries,
    upgrade_discoveries,
    worker_discovery,
)
from src.runtime.contracts import OBSERVE_ONLY, ComponentType, HealthState, LifecycleState
from src.runtime.registry import RuntimeRegistry
from src.strategies.registry import Signal, StrategyRegistry
from src.tuning.registry import ParameterRegistry, TunableParameter

S = LifecycleState


@dataclass
class _Strategy:
    strategy_id: str
    version: str = "7"

    def generate_signal(self, bar: object) -> Signal:
        return Signal(0, 0.0, 0.0)

    def required_capital_fraction(self) -> float:
        return 0.25


class _KillSwitch:
    def __init__(self, disabled: dict[str, str]) -> None:
        self._disabled = disabled

    def is_registered(self, strategy_id: str) -> bool:
        return strategy_id != "unknown-to-switch"

    def is_enabled(self, strategy_id: str) -> bool:
        return strategy_id not in self._disabled

    def disabled_reason(self, strategy_id: str) -> str:
        return self._disabled[strategy_id]


def test_strategies_map_kill_switch_state_without_touching_it() -> None:
    strategies = StrategyRegistry()
    for sid in ("trend", "carry", "unknown-to-switch"):
        strategies.register(_Strategy(sid))
    rows = {
        d.spec.component_id: d
        for d in strategy_discoveries(strategies, _KillSwitch({"carry": "drift"}))
    }
    assert rows["strategy:trend"].state is S.ACTIVE
    assert rows["strategy:carry"].state is S.QUARANTINED
    assert rows["strategy:carry"].health.detail == "drift"
    assert rows["strategy:carry"].health.state is HealthState.UNHEALTHY
    assert rows["strategy:unknown-to-switch"].state is S.ACTIVE
    trend = rows["strategy:trend"].spec
    assert trend.capabilities == OBSERVE_ONLY
    assert trend.version.version == "7"
    assert trend.version.configuration == {"required_capital_fraction": 0.25}
    assert len(strategies.all()) == 3  # the source registry is untouched
    no_switch = strategy_discoveries(strategies)
    assert {d.state for d in no_switch} == {S.ACTIVE}


def test_models_live_is_observe_only_and_shadows_can_be_promoted_or_discarded() -> None:
    models = ModelRegistry(min_evaluations=1)
    assert model_discoveries(models) == []
    models.set_live_model("btc-v31")
    models.register_shadow("btc v32")
    live, shadow = model_discoveries(models)
    assert (live.spec.component_id, live.state) == ("model:btc-v31", S.ACTIVE)
    assert live.spec.capabilities == OBSERVE_ONLY
    assert (shadow.spec.component_id, shadow.state) == ("model:btc_v32", S.STANDBY)
    assert shadow.spec.version.configuration == {"model_id": "btc v32"}
    assert shadow.spec.capabilities == SHADOW_MODEL_CAPABILITIES


@pytest.fixture(scope="module")
def orchestrator() -> EngineOrchestrator:
    return EngineOrchestrator()


def test_engine_roster_is_the_positional_order_run_uses(orchestrator: EngineOrchestrator) -> None:
    roster = orchestrator.engine_roster()
    assert [eid for eid, _ in roster] == [f"E-{i:02d}" for i in range(1, 19)]
    assert roster[8][1].endswith(".E09MlMeta")
    rows = engine_discoveries(orchestrator)
    assert [d.spec.component_id for d in rows][:2] == ["engine:E-01", "engine:E-02"]
    assert all(d.state is S.ACTIVE and d.spec.capabilities == OBSERVE_ONLY for d in rows)
    assert rows[0].spec.version.version == UNVERSIONED


def test_tuning_parameters_are_versioned_by_their_champion_value() -> None:
    params = ParameterRegistry()
    params.register(TunableParameter("hmm.entropy_threshold", "d", 0.1, 0.9, 0.5, "cpcv"))
    (row,) = tuning_discoveries(params)
    assert row.spec.component_id == "tuning:hmm.entropy_threshold"
    assert row.spec.version.version == "0.5"
    assert row.spec.version.configuration["floor"] == 0.1
    assert row.spec.capabilities == OBSERVE_ONLY


class _Upgrade:
    def list_registered(self, model_name_prefix: str = "") -> list[dict]:
        return [{"name": "price model", "latest_version": 4}]


def test_upgrade_artifacts_are_discovered_not_running() -> None:
    (row,) = upgrade_discoveries(_Upgrade())
    assert row.spec.component_id == "upgrade:price_model"
    assert (row.spec.version.version, row.state) == ("4", S.DISCOVERED)


class _Pool:
    def __init__(self, n: int) -> None:
        self.worker_count = n


@pytest.mark.parametrize(
    ("count", "state", "health"),
    [(2, S.ACTIVE, HealthState.HEALTHY), (0, S.STOPPED, HealthState.UNHEALTHY)],
)
def test_worker_pool_state_follows_worker_count(count: int, state: S, health: HealthState) -> None:
    row = worker_discovery("training", _Pool(count))
    observed = (row.spec.component_id, row.state, row.health.state)
    assert observed == ("worker:training", state, health)
    assert row.health.detail == f"{count} workers"


async def test_tasks_map_running_cancelled_failed_and_returned() -> None:
    async def forever() -> None:
        await asyncio.Event().wait()

    async def boom() -> None:
        raise RuntimeError("feed died")

    async def done() -> None:
        return None

    running = asyncio.create_task(forever(), name="loop_1h")
    cancelled = asyncio.create_task(forever(), name="midnight_reset")
    failed = asyncio.create_task(boom(), name="orderbook_stream")
    returned = asyncio.create_task(done(), name="price_preview")
    await asyncio.sleep(0)
    cancelled.cancel()
    await asyncio.gather(cancelled, failed, returned, return_exceptions=True)
    tasks = [running, cancelled, failed, returned]
    rows = {d.spec.component_id: d for d in task_discoveries(tasks)}
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    assert rows["task:loop_1h"].state is S.ACTIVE
    assert rows["task:midnight_reset"].state is S.STOPPED
    assert rows["task:orderbook_stream"].state is S.FAILED
    assert "feed died" in rows["task:orderbook_stream"].health.detail
    assert rows["task:price_preview"].health.detail == "returned"


class _Provider:
    exchange_id = "binance"


class _Bus:
    def __init__(self, dropped: int) -> None:
        self.subscriber_count = 3
        self.total_dropped = dropped


def test_providers_and_event_bus() -> None:
    (provider,) = provider_discoveries([_Provider()])
    assert provider.spec.component_id == "provider:binance"
    healthy = eventbus_discovery(_Bus(0))
    degraded = eventbus_discovery(_Bus(5), name="aux")
    assert healthy.spec.component_id == "eventbus:main"
    assert healthy.health.state is HealthState.HEALTHY
    assert degraded.health.state is HealthState.DEGRADED
    assert degraded.health.detail == "3 subscribers, 5 dropped"


@pytest.mark.parametrize(
    ("raw", "clean"), [("ok-1", "ok-1"), ("a b", "a_b"), ("_x", "x_x"), ("", "x")]
)
def test_safe_name(raw: str, clean: str) -> None:
    assert safe_name(raw) == clean


def test_every_adapter_feeds_one_registry_without_identity_clashes() -> None:
    registry = RuntimeRegistry()
    strategies = StrategyRegistry()
    strategies.register(_Strategy("trend"))
    models = ModelRegistry()
    models.set_live_model("m1")
    rows = [
        *strategy_discoveries(strategies),
        *model_discoveries(models),
        *upgrade_discoveries(_Upgrade()),
        worker_discovery("pool", _Pool(1)),
        *provider_discoveries([_Provider()]),
        eventbus_discovery(_Bus(0)),
    ]
    registry.register_all([(d.spec, d.state) for d in rows])
    assert {r.spec.component_type for r in registry.components()} == {
        ComponentType.STRATEGY,
        ComponentType.MODEL,
        ComponentType.UPGRADE,
        ComponentType.WORKER,
        ComponentType.PROVIDER,
        ComponentType.EVENTBUS,
    }
