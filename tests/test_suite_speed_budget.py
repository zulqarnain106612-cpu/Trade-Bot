"""
The suite's own running cost, ratcheted.

Sharding and caching bought back the minutes CI was wasting on installs. The
way that gets given back is one test at a time: a `sleep` to let a task settle,
a `subprocess` to run a script that could have been imported. Each one is
imperceptible on its own and none of them are ever attributed to the change
that added them -- which is exactly the failure mode GOV-015 was written for,
moved from the workflow into the suite.

So the two costs that are mechanically detectable carry a budget, and the
budget only ever goes down. A new test may not add a wall-clock stall or a
process spawn without first removing one; the honest way to pass this test is
to write the new test without them.

What the budget cannot see -- a function-scoped fixture that re-parses a file
for every case, a real network client where a fake would do, a loop that
should have been `parametrize` -- is in CLAUDE.md under "New tests are written
for speed". This file holds the part a machine can check.

Decides:
  - GOV-016 — A new test is written to run as fast as it can while still deciding its question
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS = PROJECT_ROOT / "tests"

# Frozen 2026-09-21. These may be lowered, never raised. Raising one is the
# specific thing this file exists to prevent, so a diff that does it has to
# argue for it in the commit message rather than nudging a number.
SLEEP_BUDGET = 27
SUBPROCESS_BUDGET = 14

# `sleep(0)` is a scheduler yield and costs nothing. A sleep of a minute or
# more is a sentinel inside a task the test cancels -- it is never awaited to
# completion, and awaiting it would blow the job timeout long before this
# budget mattered. Everything between the two is real wall clock.
_SLEEP = re.compile(r"(?:time|asyncio)\.sleep\(\s*([0-9.]+)\s*\)")
_SUBPROCESS = re.compile(r"subprocess\.(?:run|call|check_output|check_call|Popen)\(")

STALL_FLOOR = 0.0
SENTINEL_CEILING = 60.0
# No single stall may exceed this. Nothing in the suite does today, and a test
# that needs a tenth of a second of real time is waiting on something it should
# be controlling instead -- a clock, an event, a fake.
MAX_STALL_SECONDS = 0.1


def _test_sources() -> list[tuple[Path, str]]:
    return [
        (p, p.read_text(encoding="utf-8"))
        for p in sorted(TESTS.rglob("test_*.py"))
        if p.name != Path(__file__).name
    ]


@pytest.fixture(scope="module")
def sources() -> list[tuple[Path, str]]:
    # Module-scoped on purpose: reading three hundred files once per module is
    # cheap, once per test is the very habit this file is about.
    return _test_sources()


def _stalls(text: str) -> list[float]:
    values = [float(m.group(1)) for m in _SLEEP.finditer(text)]
    return [v for v in values if STALL_FLOOR < v < SENTINEL_CEILING]


class TestWallClockStalls:
    def test_the_suite_does_not_sleep_more_than_its_budget(self, sources):
        found = {path.name: len(stalls) for path, text in sources if (stalls := _stalls(text))}
        total = sum(found.values())
        assert total <= SLEEP_BUDGET, (
            f"{total} real sleeps, budget {SLEEP_BUDGET}. "
            f"Drive the clock or await the event instead: {found}"
        )

    def test_no_single_stall_is_long_enough_to_notice(self, sources):
        long = [
            (path.name, v) for path, text in sources for v in _stalls(text) if v > MAX_STALL_SECONDS
        ]
        assert not long, long


class TestProcessSpawns:
    def test_the_suite_does_not_spawn_more_processes_than_its_budget(self, sources):
        found = {
            path.name: len(hits) for path, text in sources if (hits := _SUBPROCESS.findall(text))
        }
        total = sum(found.values())
        assert total <= SUBPROCESS_BUDGET, (
            f"{total} process spawns, budget {SUBPROCESS_BUDGET}. "
            f"Import the module and call it instead: {found}"
        )


class TestTheBudgetIsARatchet:
    @pytest.mark.parametrize(
        ("name", "budget", "counter"),
        [
            ("sleep", SLEEP_BUDGET, lambda t: len(_stalls(t))),
            ("subprocess", SUBPROCESS_BUDGET, lambda t: len(_SUBPROCESS.findall(t))),
        ],
    )
    def test_the_budget_is_not_slack(self, sources, name, budget, counter):
        """
        A budget well above what the suite actually uses is not a ratchet, it
        is permission. Whenever the real count drops, this fails until the
        constant is lowered to match -- so the saving is banked rather than
        spent on the next test that wants a sleep.
        """
        actual = sum(counter(text) for _, text in sources)
        assert actual == budget, (
            f"{name} count is {actual} but the frozen budget says {budget}; "
            "lower the constant in this file to bank the saving"
        )
