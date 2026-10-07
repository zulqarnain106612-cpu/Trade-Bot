"""RES-021: the runtime registry is the running process, not a test fixture.

Through the real composition root (src/api/main.start_runtime_platform) with
a real Orchestrator, the registry holds E-01..E-18, consensus, the risk
quantifier, the signal gate and the ensemble, each timeframe's live model and
shadow candidate, the strategies, the self-tuning switch, the thread pools
and the event bus -- and the Runtime API serves exactly that registry. The
dependency edges are the ones the code reads, checked against the sources;
the continuous loop registers what appears after startup, records changes
its owner made and retires what its owner dropped.

Decides: RES-021"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.data.provider_cache import _ProviderCache as ProviderCache
from src.engine.orchestrator import Orchestrator
from src.engines.orchestrator import (
    ENGINE_CACHE_INPUTS,
    ENGINE_IDS,
    STAGE_INPUTS,
    EngineOrchestrator,
)
from src.runtime.changes import ChangeRequest
from src.runtime.contracts import ComponentType, LifecycleState
from src.runtime.dependencies import DependencyGraph
from src.runtime.platform import build_runtime_platform
from src.runtime.production import (
    CACHE_FIELD_PRODUCERS,
    RuntimeLoop,
    production_controllers,
    production_discoveries,
)
from src.tuning.registry import TunableParameter

from ._production import STRATEGY, World, orchestrator, signal_engine, with_shadow, world
from ._support import HUMAN, A

S = LifecycleState
ROOT = Path(__file__).resolve().parents[2]
READ = {"x-api-key": "r" * 32}

STAGES = ("consensus", "risk_quantifier", "signal_gate", "ensemble")


async def _started(w: World) -> tuple[object, object]:
    """src.api.main.start_runtime_platform over ``w`` -- the production root."""
    from src.api import main

    state = main.AppState()
    storage = AsyncMock()
    storage.fetch_runtime_desired_states.return_value = {}
    state.storage = storage
    state.orchestrator = w.orch
    sources = w.sources()
    with (
        patch.object(main, "_state", state),
        patch.object(main, "runtime_sources", lambda: sources),
    ):
        platform = await main.start_runtime_platform()
    assert platform is not None
    return state, platform


async def test_the_composition_root_registers_the_running_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    w.orch = orchestrator(
        monkeypatch, engines={"15m": await with_shadow(signal_engine())}, crypto_box=True
    )
    _, platform = await _started(w)
    ids = {r.component_id for r in platform.registry.components()}
    expected = {
        *(f"engine:{eid}" for eid in ENGINE_IDS),
        *(f"engine:{s}" for s in STAGES),
        "model:15m/live",
        "model:15m/candidate/m-2",
        f"strategy:{STRATEGY}",
        "tuning:self_tuning",
        "worker:training",
        "worker:ensemble",
        "eventbus:main",
    }
    assert expected <= ids
    graph = DependencyGraph.from_registry(platform.registry)
    assert graph.issues() == [] and graph.find_cycle() is None
    gate = platform.registry.get("engine:signal_gate").spec.dependencies
    assert [(d.component_id, d.required) for d in gate] == [
        ("engine:consensus", True),
        ("engine:risk_quantifier", True),
        ("engine:E-16", False),
    ]
    assert graph.dependents_of("engine:E-11", transitive=True) == (
        "engine:consensus",
        "engine:ensemble",
        "engine:risk_quantifier",
        "engine:signal_gate",
    )
    candidate = platform.registry.get("model:15m/candidate/m-2")
    assert candidate.state is S.STANDBY
    assert [d.component_id for d in candidate.spec.dependencies] == ["model:15m/live"]
    position = [
        platform.registry.get(f"engine:{eid}").version.configuration["position"]
        for eid in ENGINE_IDS
    ]
    assert position == list(range(1, 19))  # SIG-004: positional identity


async def test_the_runtime_api_serves_the_production_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    w.orch = orchestrator(monkeypatch, engines={"15m": signal_engine()})
    state, platform = await _started(w)
    from src.api.main import app

    env = {"API_SECRET_KEY": "t" * 32, "API_READONLY_KEY": "r" * 32, "OPERATOR_SECRET": "s" * 32}
    state.ready = True
    with patch.dict(os.environ, env), patch("src.api.main._state", state):
        client = TestClient(app, raise_server_exceptions=False)
        served = {c["component_id"] for c in client.get("/runtime/components", headers=READ).json()}
        ensemble = client.get("/runtime/components/engine:ensemble", headers=READ).json()
    assert served == {r.component_id for r in platform.registry.components()}
    assert "model:15m/live" in served and f"strategy:{STRATEGY}" in served
    # CRYPTO_BOX off: the ensemble is reported, stopped, not hidden.
    assert ensemble["state"] == "STOPPED" and ensemble["health"]["detail"].startswith("CRYPTO_BOX")


def _engine_files() -> Iterator[tuple[str, Path]]:
    for path in sorted((ROOT / "src" / "engines").glob("e[0-9][0-9]_*.py")):
        yield f"E-{path.name[1:3]}", path


def test_engine_cache_inputs_are_what_each_engine_reads() -> None:
    fields = set(ProviderCache().snapshot("BTC/USDT"))
    for engine_id, path in _engine_files():
        read = set(re.findall(r'data\.get\("([a-z_]+)"', path.read_text(encoding="utf-8")))
        assert set(ENGINE_CACHE_INPUTS.get(engine_id, ())) == read & fields, engine_id


def test_every_cache_field_but_onchain_names_its_producer() -> None:
    fields = set(ProviderCache().snapshot("BTC/USDT"))
    # Nothing in the tree calls set_onchain: E-05's input has no producer.
    assert set(CACHE_FIELD_PRODUCERS) == fields - {"onchain"}
    loops = inspect.getsource(Orchestrator._crypto_box_provider_tasks)
    keyed = set(re.findall(r'^\s+"([a-z_]+)": ', loops, re.MULTILINE))
    providers = {f for f, c in CACHE_FIELD_PRODUCERS.items() if c.startswith("provider:")}
    assert keyed == providers
    stream = (ROOT / "src" / "data" / "orderbook_stream.py").read_text(encoding="utf-8")
    assert "set_orderbook(" in stream
    assert 'name="orderbook_stream"' in inspect.getsource(Orchestrator.run)


def test_stage_inputs_are_what_the_cycle_reads() -> None:
    cycle = inspect.getsource(EngineOrchestrator.run)
    for stage, inputs in STAGE_INPUTS.items():
        for source, _ in inputs:
            if source.startswith("E-") and stage != "consensus":
                assert f'engine_map.get("{source}")' in cycle, (stage, source)
    assert {s for s, _ in STAGE_INPUTS["consensus"]} == set(ENGINE_IDS)


def _platform_and_loop(w: World) -> tuple[object, RuntimeLoop]:
    sources = w.sources()
    platform = build_runtime_platform(
        bus=w.bus,
        discoveries=production_discoveries(sources),
        controllers=production_controllers(sources),
    )
    return platform, RuntimeLoop(platform, lambda: production_discoveries(sources))


async def test_the_loop_registers_late_components_and_records_owner_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    platform, loop = _platform_and_loop(w)
    registry = platform.registry
    assert registry.find("tuning:hmm.entropy_threshold") is None
    # The scheduler registers its parameters after the platform is built.
    w.parameters.register(
        TunableParameter("hmm.entropy_threshold", "t", 0.1, 0.9, 0.5, "cpcv_oos_sharpe")
    )
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait(), name="ws_fanout")
    w.api_tasks.append(task)
    loop.step()
    assert registry.get("tuning:hmm.entropy_threshold").version.version == "0.5"
    fanout = registry.get("task:ws_fanout")
    assert fanout.state is S.ACTIVE
    assert [d.component_id for d in fanout.spec.dependencies] == ["eventbus:main"]
    w.parameters.update_current("hmm.entropy_threshold", 0.6)  # the tuning runner's promotion
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    loop.step()
    param = registry.get("tuning:hmm.entropy_threshold")
    assert param.version.version == "0.6"
    assert [v.version for v in param.previous_versions] == ["0.5"]
    assert param.last_transition is not None and param.last_transition.action is None
    assert registry.get("task:ws_fanout").state is S.STOPPED


async def test_a_dropped_candidate_is_retired_unless_a_change_holds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    engine = await with_shadow(signal_engine())
    w.orch = orchestrator(monkeypatch, engines={"15m": engine})
    platform, loop = _platform_and_loop(w)
    registry = platform.registry
    assert registry.get("model:15m/candidate/m-2").state is S.STANDBY
    # A newer candidate supersedes it (the signal engine's own rule).
    await with_shadow(engine, "m-3")
    loop.step()
    assert registry.find("model:15m/candidate/m-2") is None
    history = registry.history("model:15m/candidate/m-2")
    assert history[-1].to_state is S.STOPPED and history[-1].action is None
    held = platform.changes.submit(ChangeRequest("model:15m/candidate/m-3", A.STOP, HUMAN, "hold"))
    assert held.status.value == "APPROVED"  # LIVE_SAFE, open until executed
    await with_shadow(engine, "m-4")
    loop.step()
    # m-3 vanished too, but its open change keeps it registered.
    assert registry.get("model:15m/candidate/m-3").state is S.STOPPED
    assert registry.get("model:15m/candidate/m-4").state is S.STANDBY


async def test_one_pass_resyncs_every_discovery_without_problems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    platform, loop = _platform_and_loop(w)
    registry = platform.registry
    rows = production_discoveries(w.sources())
    assert {r.spec.component_type for r in rows} >= {
        ComponentType.EVENTBUS,
        ComponentType.STRATEGY,
        ComponentType.TUNING,
    }
    report = loop.sync_once()
    assert report.problems == () and report.seen == len(rows)
    assert len(registry.components()) == len(rows)
