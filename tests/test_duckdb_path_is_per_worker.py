"""REG-0009: every pytest process gets its own DuckDB file.

Under xdist the controller imports tests/conftest.py first and exports
DUCKDB_PATH; each worker inherits that environment, so the old
`os.environ.setdefault(...)` found the variable already set and left every
worker pointing at the controller's single file. DuckDB holds an exclusive
lock, so the first worker to open it won and the others failed with
"Conflicting lock is held in ... (PID n)".

The failure needs `-n` to appear at all, so it read as a flake: it moved
between shards and tests depending on which worker got there first. This pins
the rule that makes it impossible rather than the symptom.
"""

from __future__ import annotations

import os
import pathlib
import re

import pytest

_CONFTEST = pathlib.Path("tests/conftest.py")


@pytest.fixture(scope="module")
def conftest_source() -> str:
    return _CONFTEST.read_text(encoding="utf-8")


def test_duckdb_path_is_not_the_repository_database() -> None:
    """The original reason this conftest exists: never the real working tree."""
    path = os.environ["DUCKDB_PATH"]
    assert "data/crypto_intel.duckdb" not in path.replace(os.sep, "/").removeprefix("/tmp")
    assert pathlib.Path(path).parent.exists()


def test_this_process_owns_its_duckdb_file() -> None:
    """The worker name is in the path, so no two processes can collide."""
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker is None:
        pytest.skip("not running under xdist; the collision needs two workers")
    assert worker in os.environ["DUCKDB_PATH"]


def test_a_worker_overrides_an_inherited_path(conftest_source: str) -> None:
    """`setdefault` alone is what caused REG-0009 and must not come back.

    A worker inherits the controller's environment, so the variable is already
    set by the time the worker imports this module: the assignment has to be
    unconditional on the worker branch.
    """
    assert 'os.environ["DUCKDB_PATH"]' in conftest_source, (
        "a worker must assign DUCKDB_PATH, not setdefault it"
    )
    worker_branch = conftest_source.split("if _XDIST_WORKER:", 1)
    assert len(worker_branch) == 2, "the per-worker branch is gone"
    assert "setdefault" not in worker_branch[1].split("else:", 1)[0]


def test_the_temp_dir_name_carries_the_worker(conftest_source: str) -> None:
    assert re.search(r"mkdtemp\(prefix=f?[\"'].*\{_XDIST_WORKER", conftest_source), (
        "the temp directory must be named per worker so a leaked file is attributable"
    )
