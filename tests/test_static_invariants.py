"""
Tests for scripts/check_static_invariants.py — and the gate that runs it.

The script was written, documented, and never executed: nothing in the repo
imported it and no workflow called it, so every invariant it declares was
unenforced. That is the same "fully built, zero callers" shape the script
itself exists to detect.

Running it from pytest is what makes it real. CI already runs pytest, so the
invariants are enforced on every push without a workflow change, and a
violation names the file and line rather than failing somewhere downstream.

The per-check tests below feed each check a synthetic tree containing the
exact defect it was written for, then the same tree with the defect removed.
A check that cannot fail is worse than no check, so both directions are
asserted.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_static_invariants.py"


def _load() -> ModuleType:
    """Import the script by path — scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("check_static_invariants", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def invariants() -> ModuleType:
    return _load()


@pytest.fixture
def fake_tree(invariants, tmp_path, monkeypatch):
    """
    Point the checks at a synthetic src/ + tests/ instead of the real repo.

    Returns a writer taking a path relative to the fake repo root.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(invariants, "REPO", tmp_path)
    monkeypatch.setattr(invariants, "SRC", tmp_path / "src")

    def write(relative: str, source: str) -> None:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")

    return write


# ---------------------------------------------------------------------------
# The real repository must satisfy every invariant
# ---------------------------------------------------------------------------


def test_repository_satisfies_every_invariant(invariants) -> None:
    """
    This is the gate. A failure here names the offending file and line.
    """
    violations: list[str] = []
    for label, check in invariants.CHECKS:
        violations.extend(f"[{label}] {problem}" for problem in check())
    assert not violations, "static invariant violations:\n" + "\n".join(violations)


def test_every_check_is_registered(invariants) -> None:
    """A check absent from CHECKS never runs — the defect the script is about."""
    defined = {
        name
        for name in dir(invariants)
        if name.startswith("check_") and callable(getattr(invariants, name))
    }
    registered = {check.__name__ for _, check in invariants.CHECKS}
    assert defined == registered, f"unregistered checks: {sorted(defined - registered)}"


# ---------------------------------------------------------------------------
# check_migration_versions
# ---------------------------------------------------------------------------


def _migrations(sqlite_versions: tuple[int, ...], pg_versions: tuple[int, ...]) -> tuple[str, str]:
    def render(name: str, versions: tuple[int, ...]) -> str:
        entries = "\n".join(f'    ({v}, "desc {v}", "SQL {v}"),' for v in versions)
        return f"from typing import Final\n\n{name}: Final[list] = [\n{entries}\n]\n"

    return render("_MIGRATIONS", sqlite_versions), render("_PG_MIGRATIONS", pg_versions)


def test_duplicate_migration_version_is_flagged(invariants, fake_tree) -> None:
    sqlite, pg = _migrations((1, 2, 2), (1, 2))
    fake_tree("src/data/storage.py", sqlite)
    fake_tree("src/data/timescale_storage.py", pg)
    problems = invariants.check_migration_versions()
    assert any("version 2 defined twice" in p for p in problems)


def test_backend_migration_drift_is_flagged(invariants, fake_tree) -> None:
    """A version in one backend and not the other is two schemas, one codebase."""
    sqlite, pg = _migrations((1, 2, 3), (1, 2))
    fake_tree("src/data/storage.py", sqlite)
    fake_tree("src/data/timescale_storage.py", pg)
    problems = invariants.check_migration_versions()
    assert any("migration 3 is in" in p and "but not" in p for p in problems)


def test_matching_migration_lists_pass(invariants, fake_tree) -> None:
    sqlite, pg = _migrations((1, 2, 3), (1, 2, 3))
    fake_tree("src/data/storage.py", sqlite)
    fake_tree("src/data/timescale_storage.py", pg)
    assert invariants.check_migration_versions() == []


def test_missing_migration_list_is_reported_not_silently_passed(invariants, fake_tree) -> None:
    """A renamed list must fail loudly rather than vacuously succeed."""
    fake_tree("src/data/storage.py", "RENAMED = []\n")
    fake_tree("src/data/timescale_storage.py", "RENAMED = []\n")
    problems = invariants.check_migration_versions()
    assert len(problems) == 2
    assert all("could not read" in p for p in problems)


# ---------------------------------------------------------------------------
# check_enum_members_exist
# ---------------------------------------------------------------------------


_ENUM_SRC = """
from enum import Enum


class DiscrepancyType(Enum):
    MISSING_LOCALLY = "missing_locally"
    MISSING_IN_REFERENCE = "missing_in_reference"

    @property
    def label(self) -> str:
        return self.value
"""


def test_renamed_enum_member_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/recovery.py", _ENUM_SRC)
    fake_tree(
        "tests/test_recovery.py",
        "from src.recovery import DiscrepancyType\n"
        "def test_it():\n"
        "    assert DiscrepancyType.MISSING_ON_EXCHANGE\n",
    )
    problems = invariants.check_enum_members_exist()
    assert any("MISSING_ON_EXCHANGE is not a member" in p for p in problems)


def test_existing_enum_member_passes(invariants, fake_tree) -> None:
    fake_tree("src/recovery.py", _ENUM_SRC)
    fake_tree(
        "tests/test_recovery.py",
        "from src.recovery import DiscrepancyType\n"
        "def test_it():\n"
        "    assert DiscrepancyType.MISSING_IN_REFERENCE\n",
    )
    assert invariants.check_enum_members_exist() == []


def test_lowercase_attribute_on_an_enum_is_not_flagged(invariants, fake_tree) -> None:
    """Enums may define methods and properties; only SHOUTY names are members."""
    fake_tree("src/recovery.py", _ENUM_SRC)
    fake_tree(
        "tests/test_recovery.py",
        "from src.recovery import DiscrepancyType\n"
        "def test_it():\n"
        "    assert DiscrepancyType.MISSING_LOCALLY.label\n",
    )
    assert invariants.check_enum_members_exist() == []


# ---------------------------------------------------------------------------
# check_keyword_arguments_match_signatures
# ---------------------------------------------------------------------------


_STRATEGY_SRC = """
class OptionsCarryStrategy:
    def __init__(self, max_capital_fraction=0.1, greeks_caps=None, cfg=None):
        self._caps = greeks_caps


def helper(alpha, *, beta=None):
    return alpha, beta


class Flexible:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
"""


def test_renamed_keyword_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/strategy.py", _STRATEGY_SRC)
    fake_tree(
        "tests/test_strategy.py",
        "from src.strategy import OptionsCarryStrategy\n"
        "def test_it():\n"
        "    OptionsCarryStrategy(caps=None)\n",
    )
    problems = invariants.check_keyword_arguments_match_signatures()
    assert any("has no parameter 'caps'" in p for p in problems)


def test_accepted_keywords_pass(invariants, fake_tree) -> None:
    fake_tree("src/strategy.py", _STRATEGY_SRC)
    fake_tree(
        "tests/test_strategy.py",
        "from src.strategy import OptionsCarryStrategy, helper\n"
        "def test_it():\n"
        "    OptionsCarryStrategy(greeks_caps=None, cfg=None)\n"
        "    helper(alpha=1, beta=2)\n",
    )
    assert invariants.check_keyword_arguments_match_signatures() == []


def test_kwargs_target_is_not_checked(invariants, fake_tree) -> None:
    """**kwargs accepts anything, so no conclusion can be drawn."""
    fake_tree("src/strategy.py", _STRATEGY_SRC)
    fake_tree(
        "tests/test_strategy.py",
        "from src.strategy import Flexible\ndef test_it():\n    Flexible(anything=1)\n",
    )
    assert invariants.check_keyword_arguments_match_signatures() == []


def test_name_defined_twice_with_different_signatures_is_skipped(invariants, fake_tree) -> None:
    """
    Resolution is by bare name, so an ambiguous name must be dropped rather
    than guessed at — a false positive here would block a correct push.
    """
    fake_tree("src/a.py", "def build(alpha=None):\n    return alpha\n")
    fake_tree("src/b.py", "def build(beta=None):\n    return beta\n")
    fake_tree("tests/test_build.py", "from src.b import build\ndef test_it():\n    build(beta=1)\n")
    assert invariants.check_keyword_arguments_match_signatures() == []


# ---------------------------------------------------------------------------
# check_protocol_methods_are_called
# ---------------------------------------------------------------------------


_PROTOCOL_SRC = """
from typing import Protocol


class StrategyProtocol(Protocol):
    def generate_signal(self, bar): ...

    def required_capital_fraction(self): ...

    @property
    def strategy_id(self): ...

    def __len__(self): ...
"""


def test_protocol_method_with_no_caller_is_flagged(invariants, fake_tree) -> None:
    # The defect this check exists for: seven families implemented
    # generate_signal, a registry collected them, and nothing ever called it.
    fake_tree("src/registry.py", _PROTOCOL_SRC)
    fake_tree(
        "src/engine.py",
        "def run(registry):\n    for s in registry.all():\n        s.required_capital_fraction()\n",
    )
    problems = invariants.check_protocol_methods_are_called()
    assert any("generate_signal" in p and "never called" in p for p in problems)
    assert not any("required_capital_fraction" in p for p in problems)


def test_protocol_method_with_a_caller_passes(invariants, fake_tree) -> None:
    fake_tree("src/registry.py", _PROTOCOL_SRC)
    fake_tree(
        "src/engine.py",
        "def run(registry):\n"
        "    for s in registry.all():\n"
        "        s.generate_signal(None)\n"
        "        s.required_capital_fraction()\n"
        "        print(s.strategy_id)\n",
    )
    assert invariants.check_protocol_methods_are_called() == []


def test_protocol_property_is_checked_as_an_access_not_a_call(invariants, fake_tree) -> None:
    # A Protocol property is consumed as `obj.name`; demanding `obj.name()`
    # would report every one of them as dead.
    fake_tree("src/registry.py", _PROTOCOL_SRC)
    fake_tree(
        "src/engine.py",
        "def run(s):\n"
        "    s.generate_signal(None)\n"
        "    s.required_capital_fraction()\n"
        "    return s.strategy_id\n",
    )
    assert invariants.check_protocol_methods_are_called() == []


def test_unread_protocol_property_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/registry.py", _PROTOCOL_SRC)
    fake_tree(
        "src/engine.py",
        "def run(s):\n    s.generate_signal(None)\n    s.required_capital_fraction()\n",
    )
    problems = invariants.check_protocol_methods_are_called()
    assert any("strategy_id" in p and "never read" in p for p in problems)


def test_dunder_methods_are_exempt(invariants, fake_tree) -> None:
    # __len__ is invoked by syntax (len(x)), which an attribute scan cannot see.
    fake_tree("src/registry.py", _PROTOCOL_SRC)
    fake_tree(
        "src/engine.py",
        "def run(s):\n"
        "    s.generate_signal(None)\n"
        "    s.required_capital_fraction()\n"
        "    return s.strategy_id\n",
    )
    assert not any("__len__" in p for p in invariants.check_protocol_methods_are_called())


def test_non_protocol_class_methods_are_not_checked(invariants, fake_tree) -> None:
    # Only the Protocol contract is under scrutiny; ordinary classes have
    # plenty of legitimately internal methods.
    fake_tree(
        "src/plain.py",
        "class Ordinary:\n    def never_called_anywhere(self):\n        return 1\n",
    )
    assert invariants.check_protocol_methods_are_called() == []


# ---------------------------------------------------------------------------
# check_dataclass_fields_are_read
# ---------------------------------------------------------------------------


_DATACLASS_SRC = """
from dataclasses import dataclass


@dataclass(frozen=True)
class GateContext:
    notional_usd: float
    whale_scalar: float = 1.0
    _cache: dict | None = None
"""


def test_unread_dataclass_field_is_flagged(invariants, fake_tree) -> None:
    # The shape that hid RiskGateContext.whale_scalar: a field whose own
    # comment described machinery that did not exist.
    fake_tree("src/gates.py", _DATACLASS_SRC)
    fake_tree("src/engine.py", "def run(ctx):\n    return ctx.notional_usd\n")
    problems = invariants.check_dataclass_fields_are_read()
    assert any("whale_scalar" in p and "never read" in p for p in problems)
    assert not any("notional_usd" in p for p in problems)


def test_read_dataclass_field_passes(invariants, fake_tree) -> None:
    fake_tree("src/gates.py", _DATACLASS_SRC)
    fake_tree(
        "src/engine.py",
        "def run(ctx):\n    return ctx.notional_usd * ctx.whale_scalar\n",
    )
    assert invariants.check_dataclass_fields_are_read() == []


def test_field_used_only_as_a_constructor_kwarg_counts_as_read(invariants, fake_tree) -> None:
    # A field consumed as a kwarg or a serialisation key is genuinely used;
    # this check exists to find fields with no consumer at all.
    fake_tree("src/gates.py", _DATACLASS_SRC)
    fake_tree(
        "src/engine.py",
        "from src.gates import GateContext\n"
        "def run():\n"
        "    return GateContext(notional_usd=1.0, whale_scalar=0.5)\n",
    )
    assert invariants.check_dataclass_fields_are_read() == []


def test_a_field_read_only_by_tests_counts_as_read(invariants, fake_tree) -> None:
    fake_tree("src/gates.py", _DATACLASS_SRC)
    fake_tree("src/engine.py", "def run(ctx):\n    return ctx.notional_usd\n")
    fake_tree(
        "tests/test_gates.py",
        "def test_it(ctx):\n    assert ctx.whale_scalar == 1.0\n",
    )
    assert invariants.check_dataclass_fields_are_read() == []


def test_private_fields_are_exempt(invariants, fake_tree) -> None:
    fake_tree("src/gates.py", _DATACLASS_SRC)
    fake_tree(
        "src/engine.py",
        "def run(ctx):\n    return ctx.notional_usd * ctx.whale_scalar\n",
    )
    assert not any("_cache" in p for p in invariants.check_dataclass_fields_are_read())


def test_allowlisted_fields_are_not_reported(invariants, fake_tree) -> None:
    # The allowlist records fields whose consumer does not exist yet, so the
    # count cannot grow silently while the known ones stay visible.
    fake_tree(
        "src/labels.py",
        "from dataclasses import dataclass\n\n"
        "@dataclass\nclass TripleBarrierResult:\n    exit_index: int\n",
    )
    assert invariants.check_dataclass_fields_are_read() == []


# ---------------------------------------------------------------------------
# check_no_silent_broad_except
# ---------------------------------------------------------------------------


def test_broad_except_with_bare_pass_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/intel.py",
        "def collect(o):\n    try:\n        return o.get()\n    except Exception:\n        pass\n",
    )
    problems = invariants.check_no_silent_broad_except()
    assert any("except Exception with no handling" in p for p in problems)


def test_bare_except_with_pass_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/intel.py",
        "def collect(o):\n    try:\n        return o.get()\n    except:\n        pass\n",
    )
    assert any("bare except" in p for p in invariants.check_no_silent_broad_except())


def test_broad_except_with_continue_is_flagged(invariants, fake_tree) -> None:
    # continue discards the exception just as completely as pass.
    fake_tree(
        "src/fit.py",
        "def fit(xs):\n"
        "    for x in xs:\n"
        "        try:\n"
        "            score(x)\n"
        "        except Exception:\n"
        "            continue\n",
    )
    assert invariants.check_no_silent_broad_except() != []


def test_narrow_except_with_pass_is_exempt(invariants, fake_tree) -> None:
    # `except ImportError: pass` around an optional dependency is control
    # flow, and the exception type documents the intent.
    fake_tree(
        "src/agent.py",
        "def setup():\n    try:\n        import gym\n    except ImportError:\n        pass\n",
    )
    assert invariants.check_no_silent_broad_except() == []


def test_broad_except_that_logs_is_not_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/intel.py",
        "def collect(o):\n"
        "    try:\n"
        "        return o.get()\n"
        "    except Exception as exc:\n"
        "        log.warning('failed', error=str(exc))\n",
    )
    assert invariants.check_no_silent_broad_except() == []


def test_broad_except_that_returns_a_fallback_is_not_flagged(invariants, fake_tree) -> None:
    # Returning a documented fallback is handling, not swallowing.
    fake_tree(
        "src/intel.py",
        "def collect(o):\n"
        "    try:\n"
        "        return o.get()\n"
        "    except Exception:\n"
        "        return None\n",
    )
    assert invariants.check_no_silent_broad_except() == []


def test_docstring_only_body_does_not_mask_the_pass(invariants, fake_tree) -> None:
    fake_tree(
        "src/intel.py",
        "def collect(o):\n"
        "    try:\n"
        "        return o.get()\n"
        "    except Exception:\n"
        "        'why this is fine'\n"
        "        pass\n",
    )
    assert invariants.check_no_silent_broad_except() != []


# ---------------------------------------------------------------------------
# check_datetimes_are_timezone_aware
# ---------------------------------------------------------------------------


def test_utcnow_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/clock.py",
        "from datetime import datetime\ndef t():\n    return datetime.utcnow()\n",
    )
    assert any("utcnow" in p for p in invariants.check_datetimes_are_timezone_aware())


def test_naive_now_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/clock.py",
        "from datetime import datetime\ndef t():\n    return datetime.now()\n",
    )
    assert invariants.check_datetimes_are_timezone_aware() != []


def test_aware_now_passes(invariants, fake_tree) -> None:
    fake_tree(
        "src/clock.py",
        "from datetime import UTC, datetime\ndef t():\n    return datetime.now(tz=UTC)\n",
    )
    assert invariants.check_datetimes_are_timezone_aware() == []


def test_fromtimestamp_with_positional_tz_passes(invariants, fake_tree) -> None:
    # tz is the second POSITIONAL parameter; this is how the real codebase
    # writes it, and reading it as naive would be a false positive.
    fake_tree(
        "src/clock.py",
        "from datetime import UTC, datetime\ndef t():\n    return datetime.fromtimestamp(0, UTC)\n",
    )
    assert invariants.check_datetimes_are_timezone_aware() == []


def test_fromtimestamp_without_tz_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/clock.py",
        "from datetime import datetime\ndef t():\n    return datetime.fromtimestamp(0)\n",
    )
    assert invariants.check_datetimes_are_timezone_aware() != []


# ---------------------------------------------------------------------------
# check_no_mutable_default_arguments
# ---------------------------------------------------------------------------


def test_mutable_list_default_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/acc.py", "def add(x, acc=[]):\n    acc.append(x)\n    return acc\n")
    assert any("mutable default" in p for p in invariants.check_no_mutable_default_arguments())


def test_mutable_dict_and_set_defaults_are_flagged(invariants, fake_tree) -> None:
    fake_tree("src/acc.py", "def a(d={}):\n    return d\ndef b(s=set()):\n    return s\n")
    assert len(invariants.check_no_mutable_default_arguments()) == 2


def test_keyword_only_mutable_default_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/acc.py", "def add(x, *, acc=[]):\n    return acc\n")
    assert invariants.check_no_mutable_default_arguments() != []


def test_immutable_defaults_pass(invariants, fake_tree) -> None:
    fake_tree(
        "src/acc.py",
        "def f(a=None, b=0, c=(), d='x', e=1.0):\n    return a, b, c, d, e\n",
    )
    assert invariants.check_no_mutable_default_arguments() == []


def test_default_factory_pattern_passes(invariants, fake_tree) -> None:
    fake_tree(
        "src/acc.py",
        "from dataclasses import dataclass, field\n\n"
        "@dataclass\nclass C:\n    items: list = field(default_factory=list)\n",
    )
    assert invariants.check_no_mutable_default_arguments() == []


# ---------------------------------------------------------------------------
# check_durations_use_monotonic
# ---------------------------------------------------------------------------


def test_wall_clock_subtraction_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/age.py",
        "import time\ndef age(start):\n    return (time.time() - start) / 3600.0\n",
    )
    problems = invariants.check_durations_use_monotonic()
    assert any("used as a duration" in p for p in problems)


def test_wall_clock_deadline_comparison_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/collect.py",
        "import time\n"
        "def collect(q):\n"
        "    deadline = time.time() + 5.0\n"
        "    while time.time() < deadline:\n"
        "        q.get()\n",
    )
    assert invariants.check_durations_use_monotonic() != []


def test_recording_a_timestamp_is_not_flagged(invariants, fake_tree) -> None:
    # Wall clock is correct for WHEN something happened. Only arithmetic and
    # comparison — the shape of "using this as a duration" — are flagged.
    fake_tree(
        "src/emit.py",
        "import time\ndef emit(bus):\n    bus.send({'ts': int(time.time() * 1000)})\n",
    )
    assert invariants.check_durations_use_monotonic() == []


def test_monotonic_arithmetic_passes(invariants, fake_tree) -> None:
    fake_tree(
        "src/age.py",
        "import time\ndef age(start):\n    return time.monotonic() - start\n",
    )
    assert invariants.check_durations_use_monotonic() == []


def test_allowlisted_epoch_arithmetic_is_not_flagged(invariants, fake_tree, monkeypatch) -> None:
    # Computing a point in history by subtraction is real and correct; the
    # exception is allowlisted rather than special-cased in the check.
    monkeypatch.setattr(
        invariants,
        "_WALL_CLOCK_ARITHMETIC_ALLOWED",
        {("src/feed.py", "_window")},
    )
    fake_tree(
        "src/feed.py",
        "import time\ndef _window(days):\n    return int((time.time() - days * 86400) * 1000)\n",
    )
    assert invariants.check_durations_use_monotonic() == []


# ---------------------------------------------------------------------------
# check_cpu_bound_work_is_offloaded
# ---------------------------------------------------------------------------


def test_inline_cpu_bound_call_in_async_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/engine.py",
        "async def tick(bars):\n    fm = build_feature_matrix(bars)\n    return fm\n",
    )
    problems = invariants.check_cpu_bound_work_is_offloaded()
    assert any("called inline in async tick()" in p for p in problems)


def test_to_thread_offload_passes(invariants, fake_tree) -> None:
    fake_tree(
        "src/engine.py",
        "import asyncio\n"
        "async def tick(bars):\n"
        "    return await asyncio.to_thread(build_feature_matrix, bars)\n",
    )
    assert invariants.check_cpu_bound_work_is_offloaded() == []


def test_run_in_executor_offload_passes(invariants, fake_tree) -> None:
    fake_tree(
        "src/train.py",
        "async def train(loop, bars):\n"
        "    return await loop.run_in_executor(None, build_feature_matrix, bars)\n",
    )
    assert invariants.check_cpu_bound_work_is_offloaded() == []


def test_a_synchronous_caller_is_not_flagged(invariants, fake_tree) -> None:
    # Blocking a sync function blocks only its caller; the loop is the issue.
    fake_tree("src/backtest.py", "def run(bars):\n    return build_feature_matrix(bars)\n")
    assert invariants.check_cpu_bound_work_is_offloaded() == []


# ---------------------------------------------------------------------------
# Blind-spot regressions
#
# Each check below was probed with an ALTERNATIVE SPELLING of the very defect
# it was written to catch, and each let it through. A check that reports clean
# while its defect sits in the tree is worse than no check, because it buys
# confidence it has not earned — the datetime form of a wall-clock duration
# did exactly that for ten iterations.
# ---------------------------------------------------------------------------


def test_broad_except_with_a_bare_return_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/a.py",
        "def f(o):\n    try:\n        o.get()\n    except Exception:\n        return\n",
    )
    assert invariants.check_no_silent_broad_except() != []


def test_broad_except_with_ellipsis_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/a.py",
        "def f(o):\n    try:\n        o.get()\n    except Exception:\n        ...\n",
    )
    assert invariants.check_no_silent_broad_except() != []


def test_broad_except_returning_a_value_is_still_not_flagged(invariants, fake_tree) -> None:
    # `return None` explicitly is handling; a bare `return` is a swallow.
    fake_tree(
        "src/a.py",
        "def f(o):\n    try:\n        return o.get()\n    except Exception:\n        return None\n",
    )
    assert invariants.check_no_silent_broad_except() == []


def test_discarded_ensure_future_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/a.py", "import asyncio\nasync def f():\n    asyncio.ensure_future(g())\n")
    assert invariants.check_no_orphan_tasks() != []


def test_retained_ensure_future_is_not_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/a.py",
        "import asyncio\n"
        "async def f():\n"
        "    t = asyncio.ensure_future(g())\n"
        "    t.add_done_callback(h)\n",
    )
    assert invariants.check_no_orphan_tasks() == []


def test_generic_protocol_methods_are_checked(invariants, fake_tree) -> None:
    # class P(Protocol[T]) has a Subscript base, not a Name.
    fake_tree(
        "src/a.py",
        "from typing import Protocol, TypeVar\n"
        "T = TypeVar('T')\n"
        "class P(Protocol[T]):\n"
        "    def never_called(self): ...\n",
    )
    problems = invariants.check_protocol_methods_are_called()
    assert any("never_called" in p for p in problems)


def test_cpu_bound_call_via_attribute_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/a.py",
        "import pipeline\nasync def tick(b):\n    return pipeline.build_feature_matrix(b)\n",
    )
    assert invariants.check_cpu_bound_work_is_offloaded() != []


def test_cpu_bound_attribute_call_offloaded_passes(invariants, fake_tree) -> None:
    fake_tree(
        "src/a.py",
        "import asyncio, pipeline\n"
        "async def tick(b):\n"
        "    return await asyncio.to_thread(pipeline.build_feature_matrix, b)\n",
    )
    assert invariants.check_cpu_bound_work_is_offloaded() == []


def test_defaultdict_default_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/a.py",
        "from collections import defaultdict\ndef f(a=defaultdict(list)):\n    return a\n",
    )
    assert invariants.check_no_mutable_default_arguments() != []


def test_deque_default_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/a.py",
        "from collections import deque\ndef f(a=deque(maxlen=5)):\n    return a\n",
    )
    assert invariants.check_no_mutable_default_arguments() != []


def test_datetime_module_qualified_duration_is_flagged(invariants, fake_tree) -> None:
    # `import datetime` then datetime.datetime.now() — the owner is an
    # Attribute, not a Name, and slipped past until probed for.
    fake_tree(
        "src/a.py",
        "import datetime\ndef f(s):\n    return (datetime.datetime.now() - s).total_seconds()\n",
    )
    assert invariants.check_durations_use_monotonic() != []


def test_positional_slice_through_list_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/a.py", "def f(df, n):\n    return list(df.columns)[:n]\n")
    assert invariants.check_positional_column_slices() != []


def test_positional_slice_through_tolist_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/a.py", "def f(df, n):\n    return df.columns.tolist()[:n]\n")
    assert invariants.check_positional_column_slices() != []


def test_slicing_a_non_axis_list_is_not_flagged(invariants, fake_tree) -> None:
    # The check is name-based; list() of something that is not .columns or
    # .index must stay clean or every slice in the codebase lights up.
    fake_tree("src/a.py", "def f(x, n):\n    return list(x.values)[:n]\n")
    assert invariants.check_positional_column_slices() == []


# ---------------------------------------------------------------------------
# check_no_mutable_class_attributes
# ---------------------------------------------------------------------------


def test_populated_class_dict_is_flagged(invariants, fake_tree) -> None:
    # The shape that left OrderFSM._VALID_TRANSITIONS writable: Final stops
    # rebinding, not mutation, and that table gates order state changes.
    fake_tree(
        "src/fsm.py",
        "from typing import Final\nclass FSM:\n    T: Final[dict] = {'a': 1}\n",
    )
    problems = invariants.check_no_mutable_class_attributes()
    assert any("FSM.T" in p and "mutable class attribute" in p for p in problems)


def test_populated_class_list_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/fsm.py", "class FSM:\n    T = [1]\n")
    assert invariants.check_no_mutable_class_attributes() != []


def test_mappingproxy_passes(invariants, fake_tree) -> None:
    fake_tree(
        "src/fsm.py",
        "from types import MappingProxyType\nclass FSM:\n    T = MappingProxyType({'a': 1})\n",
    )
    assert invariants.check_no_mutable_class_attributes() == []


def test_immutable_containers_pass(invariants, fake_tree) -> None:
    fake_tree("src/fsm.py", "class FSM:\n    A = (1, 2)\n    B = frozenset({1})\n")
    assert invariants.check_no_mutable_class_attributes() == []


def test_empty_literal_is_exempt(invariants, fake_tree) -> None:
    # The conventional spelling of "no entries by default".
    fake_tree("src/fsm.py", "class FSM:\n    T = {}\n")
    assert invariants.check_no_mutable_class_attributes() == []


def test_dataclass_fields_are_skipped(invariants, fake_tree) -> None:
    # field(default_factory=...) is their answer; the mutable-defaults rule
    # covers that separately.
    fake_tree(
        "src/rec.py",
        "from dataclasses import dataclass, field\n\n"
        "@dataclass\nclass Rec:\n    items: list = field(default_factory=list)\n",
    )
    assert invariants.check_no_mutable_class_attributes() == []


# ---------------------------------------------------------------------------
# check_docstrings_do_not_cite_missing_modules
# ---------------------------------------------------------------------------


def test_docstring_citing_a_phantom_module_is_flagged(invariants, fake_tree) -> None:
    # derivatives.py claimed to consolidate a Deribit provider that has never
    # existed — and that absence is why options_carry_v1 is inert.
    fake_tree("src/feat.py", '"""Consolidates deribit_provider.py."""\n')
    problems = invariants.check_docstrings_do_not_cite_missing_modules()
    assert any("deribit_provider.py" in p for p in problems)


def test_a_real_sibling_module_passes(invariants, fake_tree) -> None:
    fake_tree("src/feat.py", '"""See helper.py."""\n')
    fake_tree("src/helper.py", "")
    assert invariants.check_docstrings_do_not_cite_missing_modules() == []


def test_a_real_file_under_scripts_passes(invariants, fake_tree, tmp_path) -> None:
    # src/ and scripts/ reference each other; resolving against src/ alone
    # produced six false positives on the first pass.
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "run_tuning_attempt.py").write_text("")
    fake_tree("src/sched.py", '"""Spawns run_tuning_attempt.py."""\n')
    assert invariants.check_docstrings_do_not_cite_missing_modules() == []


def test_a_glob_family_is_not_treated_as_a_filename(invariants, fake_tree) -> None:
    # `*_provider.py` names a family; capturing `_provider.py` out of it
    # reports a file nobody claimed exists.
    fake_tree("src/mac.py", '"""See providers/*_provider.py."""\n')
    assert invariants.check_docstrings_do_not_cite_missing_modules() == []


def test_function_docstrings_are_checked_too(invariants, fake_tree) -> None:
    fake_tree("src/feat.py", 'def f():\n    """Calls ghost_module.py."""\n')
    assert invariants.check_docstrings_do_not_cite_missing_modules() != []


# ---------------------------------------------------------------------------
# check_every_route_is_authenticated
# ---------------------------------------------------------------------------


def test_an_unguarded_route_is_flagged(invariants, fake_tree) -> None:
    # The failure is silent in the worst direction: nothing in the response,
    # the logs or the type checker distinguishes an unguarded endpoint.
    fake_tree("src/api/main.py", '@app.get("/secrets")\nasync def s():\n    return 1\n')
    problems = invariants.check_every_route_is_authenticated()
    assert any("/secrets" in p and "serves unauthenticated" in p for p in problems)


def test_a_dependencies_guard_passes(invariants, fake_tree) -> None:
    fake_tree(
        "src/api/main.py",
        '@app.get("/s", dependencies=[Depends(api_key_header)])\nasync def s():\n    return 1\n',
    )
    assert invariants.check_every_route_is_authenticated() == []


def test_an_unguarded_mutating_route_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/api/main.py", '@app.post("/act")\nasync def a():\n    return 1\n')
    assert invariants.check_every_route_is_authenticated() != []


def test_in_body_websocket_auth_passes(invariants, fake_tree) -> None:
    # The real /ws awaits verify_ws_key() first on purpose — auth must run
    # before add_ws_client(), or a raising auth leaks a client slot.
    # Demanding the decorator form would push someone to "fix" that ordering.
    fake_tree(
        "src/api/main.py",
        '@app.websocket("/ws")\nasync def w(ws):\n    await verify_ws_key(ws)\n    return 1\n',
    )
    assert invariants.check_every_route_is_authenticated() == []


def test_a_websocket_with_no_auth_at_all_is_flagged(invariants, fake_tree) -> None:
    fake_tree(
        "src/api/main.py",
        '@app.websocket("/ws")\nasync def w(ws):\n    await ws.accept()\n',
    )
    assert invariants.check_every_route_is_authenticated() != []


def test_non_route_functions_are_ignored(invariants, fake_tree) -> None:
    fake_tree("src/api/main.py", "def helper():\n    return 1\n")
    assert invariants.check_every_route_is_authenticated() == []


def test_a_setting_read_only_by_a_test_is_still_flagged(invariants, fake_tree) -> None:
    # The loophole this check used to have: counting tests/ as readers let a
    # setting satisfy it while the software still ignored the value. A test
    # asserting a default exists proves nothing about anything consuming it.
    fake_tree(
        "src/config.py",
        "from pydantic import Field\nfrom pydantic_settings import BaseSettings\n\n"
        "class DemoSettings(BaseSettings):\n    widget_size: int = Field(default=3)\n",
    )
    fake_tree("tests/test_widget.py", "def test_it(cfg):\n    assert cfg.widget_size == 3\n")
    assert any("widget_size" in p for p in invariants.check_settings_are_read())


def test_a_setting_read_in_src_passes(invariants, fake_tree) -> None:
    fake_tree(
        "src/config.py",
        "from pydantic import Field\nfrom pydantic_settings import BaseSettings\n\n"
        "class DemoSettings(BaseSettings):\n    widget_size: int = Field(default=3)\n",
    )
    fake_tree("src/use.py", "def f(cfg):\n    return cfg.widget_size\n")
    assert invariants.check_settings_are_read() == []


def test_a_bare_string_literal_no_longer_counts_as_a_field_read(invariants, fake_tree) -> None:
    # Too weak a proxy: any log key or message fragment matching the field
    # name rescued a field nothing touches. ModelRecord.predictions survived
    # on exactly that, in a class whose two siblings were already known dead.
    fake_tree(
        "src/rec.py",
        "from dataclasses import dataclass\n\n@dataclass\nclass Rec:\n    widget: int = 0\n",
    )
    fake_tree("src/log.py", 'MESSAGE = "widget"\n')
    assert any("Rec.widget" in p for p in invariants.check_dataclass_fields_are_read())


def test_kwarg_construction_still_counts_as_a_field_read(invariants, fake_tree) -> None:
    # A field consumed only as a constructor kwarg is genuinely used.
    fake_tree(
        "src/rec.py",
        "from dataclasses import dataclass\n\n@dataclass\nclass Rec:\n    widget: int = 0\n",
    )
    fake_tree("src/use.py", "from src.rec import Rec\ndef f():\n    return Rec(widget=1)\n")
    assert invariants.check_dataclass_fields_are_read() == []


# check_dataclass_attributes_exist
# ---------------------------------------------------------------------------

_RESULT_SRC = """
from dataclasses import dataclass


@dataclass
class WorkerResult:
    horizon_id: int
    confidence: float

    def label(self) -> str:
        return str(self.horizon_id)


@dataclass
class Bag:
    items: list


@dataclass
class Derived(Bag):
    extra: int
"""


def test_renamed_dataclass_field_is_flagged(invariants, fake_tree) -> None:
    fake_tree("src/workers.py", _RESULT_SRC)
    fake_tree(
        "src/reader.py",
        "from src.workers import WorkerResult\n"
        "def read():\n"
        "    r = WorkerResult(horizon_id=1, confidence=0.5)\n"
        "    return r.horizon_idx\n",
    )
    problems = invariants.check_dataclass_attributes_exist()
    assert len(problems) == 1
    assert "WorkerResult has no attribute 'horizon_idx'" in problems[0]


def test_existing_field_and_method_pass(invariants, fake_tree) -> None:
    """Methods defined on the dataclass are reachable too, not just fields."""
    fake_tree("src/workers.py", _RESULT_SRC)
    fake_tree(
        "src/reader.py",
        "from src.workers import WorkerResult\n"
        "def read():\n"
        "    r = WorkerResult(horizon_id=1, confidence=0.5)\n"
        "    return r.horizon_id, r.confidence, r.label()\n",
    )
    assert invariants.check_dataclass_attributes_exist() == []


def test_field_reached_through_an_annotated_list_is_flagged(invariants, fake_tree) -> None:
    """
    The real defect took this shape: a list built up over a loop, then
    ``max()``-ed, then read. Nothing in between states the type but the
    annotation on the empty list.
    """
    fake_tree("src/workers.py", _RESULT_SRC)
    fake_tree(
        "src/reader.py",
        "from src.workers import WorkerResult\n"
        "def read(source):\n"
        "    results: list[WorkerResult] = []\n"
        "    for item in source:\n"
        "        results.append(item)\n"
        "    best = max(results, key=lambda r: r.confidence)\n"
        "    return best.horizon_idx\n",
    )
    problems = invariants.check_dataclass_attributes_exist()
    assert len(problems) == 1
    assert "has no attribute 'horizon_idx'" in problems[0]


def test_subclass_is_not_checked(invariants, fake_tree) -> None:
    """Inherited attributes are invisible to an AST pass — do not guess."""
    fake_tree("src/workers.py", _RESULT_SRC)
    fake_tree(
        "src/reader.py",
        "from src.workers import Derived\n"
        "def read():\n"
        "    d = Derived(items=[], extra=1)\n"
        "    return d.items\n",
    )
    assert invariants.check_dataclass_attributes_exist() == []


def test_rebound_name_is_dropped(invariants, fake_tree) -> None:
    """Once a name holds something else, nothing can be concluded about it."""
    fake_tree("src/workers.py", _RESULT_SRC)
    fake_tree(
        "src/reader.py",
        "from src.workers import WorkerResult\n"
        "def read(other):\n"
        "    r = WorkerResult(horizon_id=1, confidence=0.5)\n"
        "    r = other\n"
        "    return r.anything_at_all\n",
    )
    assert invariants.check_dataclass_attributes_exist() == []


def test_non_dataclass_is_not_checked(invariants, fake_tree) -> None:
    fake_tree("src/plain.py", "class Plain:\n    def __init__(self):\n        self.x = 1\n")
    fake_tree(
        "src/reader.py",
        "from src.plain import Plain\ndef read():\n    p = Plain()\n    return p.whatever\n",
    )
    assert invariants.check_dataclass_attributes_exist() == []
