"""
The eighteen-engine seam, asserted from both sides.

`src/engines/` is the widest hand-off in the system: eighteen independent
producers, one consumer. `EngineOrchestrator.run` gathers them and then
attributes the results **by position** --

    zip(self._engines, [f"E-{i:02d}" for i in range(1, 19)], strict=True)
    ...
    eid = f"E-{i + 1:02d}"

-- so `self._engines[3]` is *assumed* to be E-04. Nothing in the code says so.
Reorder that list, or insert an engine in the middle, and every output is
filed under the wrong engine id, every SLA is applied to the wrong engine, and
every log line names the wrong thing. No exception is raised and no test that
checks one engine in isolation notices, because each engine is still correct.
That is the failure this file exists for: two sides agreeing by position while
disagreeing about meaning.

So the contract is pinned from both ends -- what every producer must emit, and
what the consumer must receive -- against the same fixture, and the positional
assumption is made explicit instead of implied.
"""

from __future__ import annotations

import importlib
import inspect
import math
import re
from datetime import datetime
from pathlib import Path

import pytest

from src.engines.orchestrator import _ENGINE_SLAS, EngineOrchestrator, OrchestratorResult
from src.engines.schema import EngineOutput

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENGINES_DIR = PROJECT_ROOT / "src" / "engines"

ENGINE_COUNT = 18
VALID_DIRECTIONS = {-1, 0, 1}
SYMBOL = "BTC/USDT"


def _engine_modules() -> list[tuple[str, str]]:
    """(module stem, expected engine id) for every eNN_*.py, in numeric order."""
    found = []
    for path in sorted(ENGINES_DIR.glob("e[0-9][0-9]_*.py")):
        number = int(path.stem[1:3])
        found.append((path.stem, f"E-{number:02d}"))
    return found


@pytest.fixture(scope="module")
def engine_modules() -> list[tuple[str, str]]:
    return _engine_modules()


@pytest.fixture(scope="module")
def orchestrator() -> EngineOrchestrator:
    # Module-scoped: constructing it is the composition root, and doing that
    # once for the whole file is the point of the smoke test below.
    return EngineOrchestrator()


def _engine_class(stem: str):
    module = importlib.import_module(f"src.engines.{stem}")
    classes = [
        obj
        for obj in vars(module).values()
        if inspect.isclass(obj) and obj.__module__ == module.__name__ and hasattr(obj, "run")
    ]
    assert len(classes) == 1, f"{stem}: expected one engine class, found {classes}"
    return classes[0]


def _assert_contract(out: object, expected_id: str) -> None:
    """
    Everything the consumer relies on. Asserted identically wherever an output
    crosses the seam, so producer and consumer cannot drift apart.
    """
    assert isinstance(out, EngineOutput), type(out)
    assert out.engine_id == expected_id, (out.engine_id, expected_id)
    assert out.symbol == SYMBOL
    assert isinstance(out.timestamp_utc, datetime)
    # A naive timestamp compared against an aware one raises at the point of
    # comparison, which is inside the consumer, long after the producer is off
    # the stack.
    assert out.timestamp_utc.tzinfo is not None, expected_id
    assert isinstance(out.predicted_price, float), expected_id
    assert math.isfinite(out.predicted_price), (expected_id, out.predicted_price)
    assert 0.0 <= out.confidence <= 1.0, (expected_id, out.confidence)
    assert out.direction in VALID_DIRECTIONS, (expected_id, out.direction)
    assert isinstance(out.horizon_hours, int) and out.horizon_hours > 0, expected_id
    assert isinstance(out.metadata, dict), expected_id


class TestTheProducerSide:
    def test_there_are_exactly_eighteen_engine_modules(self, engine_modules):
        """
        The consumer hard-codes `range(1, 19)` with `strict=True`. A nineteenth
        engine file that nobody wires in is invisible; a missing one turns the
        zip into a runtime error at the worst moment.
        """
        assert len(engine_modules) == ENGINE_COUNT
        assert [eid for _, eid in engine_modules] == [
            f"E-{i:02d}" for i in range(1, ENGINE_COUNT + 1)
        ]

    @pytest.mark.parametrize(("stem", "expected_id"), _engine_modules())
    def test_the_declared_id_matches_the_filename(self, stem, expected_id):
        """
        `e07_linear_algebra.py` declaring `E-08` would misfile every one of its
        outputs, and the only place the two are related is a naming convention.
        """
        source = (ENGINES_DIR / f"{stem}.py").read_text(encoding="utf-8")
        declared = re.search(r'^_ENGINE_ID\s*=\s*"([^"]+)"', source, re.MULTILINE)
        assert declared, f"{stem}: no module-level _ENGINE_ID"
        assert declared.group(1) == expected_id

    @pytest.mark.parametrize(("stem", "expected_id"), _engine_modules())
    def test_every_engine_offers_the_same_call(self, stem, expected_id):
        """
        The consumer calls `engine.run(symbol, data)` on all eighteen without
        looking. One engine with a different parameter name or an extra
        required argument breaks only that engine, only at runtime, and only
        as a logged warning.
        """
        engine = _engine_class(stem)
        signature = inspect.signature(engine.run)
        assert list(signature.parameters) == ["self", "symbol", "data"], stem
        assert inspect.iscoroutinefunction(engine.run), stem

    @pytest.mark.parametrize(("stem", "expected_id"), _engine_modules())
    async def test_an_engine_given_nothing_still_honours_the_contract(self, stem, expected_id):
        """
        Degradation is part of the seam, not an exception to it. An engine with
        no data must abstain into a valid EngineOutput -- raising would be
        caught by the gather and turned into a silently missing engine.
        """
        out = await _engine_class(stem)().run(SYMBOL, {})
        _assert_contract(out, expected_id)


class TestTheConsumerSide:
    def test_the_composition_root_registers_every_engine(self, orchestrator):
        """The smoke test: the thing that assembles the pipeline assembles."""
        assert len(orchestrator._engines) == ENGINE_COUNT

    def test_position_in_the_list_agrees_with_the_engine_it_holds(
        self, orchestrator, engine_modules
    ):
        """
        This is the one that matters. `run` attributes result `i` to
        `E-{i+1:02d}` by position. Reorder the list and every output is filed
        under the wrong engine, every SLA lands on the wrong engine, and
        nothing raises.
        """
        registered = [type(e).__module__.rsplit(".", 1)[-1] for e in orchestrator._engines]
        assert registered == [stem for stem, _ in engine_modules]

    def test_every_engine_has_an_sla_and_every_sla_an_engine(self, engine_modules):
        """
        `_ENGINE_SLAS.get(eid, 5.0)` means a typo'd key silently becomes the
        default, so an engine given 2 seconds on purpose quietly gets five.
        """
        assert set(_ENGINE_SLAS) == {eid for _, eid in engine_modules}
        assert all(t > 0 for t in _ENGINE_SLAS.values())


class TestTheSeamEndToEnd:
    async def test_a_cycle_accounts_for_every_engine(self, orchestrator):
        """
        One pass through the real seam: eighteen producers, the consumer, and
        consensus. Every engine must come back either as an output or as a
        named failure -- an engine that is neither has been dropped, which is
        the loss no per-engine test can see.
        """
        result = await orchestrator.run(SYMBOL, {})

        assert isinstance(result, OrchestratorResult)
        assert result.symbol == SYMBOL
        assert result.timestamp_utc.tzinfo is not None

        accounted = {o.engine_id for o in result.engine_outputs} | set(result.failed_engines)
        assert accounted == {f"E-{i:02d}" for i in range(1, ENGINE_COUNT + 1)}
        assert len(result.engine_outputs) + len(result.failed_engines) == ENGINE_COUNT

    async def test_nothing_is_dropped_and_nothing_is_duplicated(self, orchestrator):
        ids = [o.engine_id for o in (await orchestrator.run(SYMBOL, {})).engine_outputs]
        assert len(ids) == len(set(ids))

    async def test_every_output_that_reaches_consensus_honours_the_contract(self, orchestrator):
        """
        The same assertions as the producer side, applied where the consumer
        actually receives them. If these two ever diverge, the seam has a gap
        in it and this is where it shows.
        """
        for out in (await orchestrator.run(SYMBOL, {})).engine_outputs:
            _assert_contract(out, out.engine_id)
