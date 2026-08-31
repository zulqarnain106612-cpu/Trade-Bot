"""Coverage for TimescaleBackend.initialize() and _run_migrations().

No PostgreSQL/TimescaleDB instance is involved: asyncpg.create_pool is
patched and the connection is an AsyncMock, so these tests assert the
control flow (DDL order, pool cleanup on failure, migration gating)
rather than the SQL's effect on a real database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.data.timescale_storage as ts_mod
from src.data.timescale_storage import _PG_SCHEMA_VERSION, TimescaleBackend


def _conn() -> AsyncMock:
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=_PG_SCHEMA_VERSION)
    conn.transaction = MagicMock(return_value=AsyncMock())
    return conn


def _pool(conn: AsyncMock) -> MagicMock:
    pool = MagicMock()
    acquire_ctx = AsyncMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_ctx)
    pool.close = AsyncMock()
    return pool


def _backend(tmp_path) -> TimescaleBackend:
    settings = MagicMock()
    settings.storage.db_path = tmp_path / "db" / "trade.db"
    settings.storage.model_dir = tmp_path / "models"
    settings.storage.log_dir = tmp_path / "logs"
    settings.storage.timescale_dsn = "postgresql://user:pw@localhost/db"
    with patch.object(ts_mod, "get_settings", return_value=settings):
        return TimescaleBackend()


def test_get_lock_is_created_once_and_reused(tmp_path):
    backend = _backend(tmp_path)
    assert backend._lock is None
    first = backend._get_lock()
    second = backend._get_lock()
    assert first is second


async def test_initialize_creates_directories_and_applies_ddl(tmp_path):
    backend = _backend(tmp_path)
    conn = _conn()
    pool = _pool(conn)

    settings = MagicMock()
    settings.storage.db_path = tmp_path / "db" / "trade.db"
    settings.storage.model_dir = tmp_path / "models"
    settings.storage.log_dir = tmp_path / "logs"

    with (
        patch.object(ts_mod, "get_settings", return_value=settings),
        patch.object(ts_mod.asyncpg, "create_pool", AsyncMock(return_value=pool)),
    ):
        await backend.initialize()

    assert (tmp_path / "models").is_dir()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "db").is_dir()
    assert backend._pool is pool
    # TimescaleDB extension must be created before the DDL that depends on it.
    executed = [c.args[0] for c in conn.execute.await_args_list]
    assert "CREATE EXTENSION IF NOT EXISTS timescaledb" in executed[0]


async def test_initialize_is_idempotent_when_pool_already_exists(tmp_path):
    backend = _backend(tmp_path)
    existing = MagicMock()
    backend._pool = existing

    settings = MagicMock()
    settings.storage.db_path = tmp_path / "db" / "trade.db"
    settings.storage.model_dir = tmp_path / "models"
    settings.storage.log_dir = tmp_path / "logs"

    with (
        patch.object(ts_mod, "get_settings", return_value=settings),
        patch.object(ts_mod.asyncpg, "create_pool", AsyncMock()) as mock_create,
    ):
        await backend.initialize()

    mock_create.assert_not_awaited()
    assert backend._pool is existing


async def test_initialize_closes_pool_when_ddl_fails(tmp_path):
    backend = _backend(tmp_path)
    conn = _conn()
    conn.execute = AsyncMock(side_effect=RuntimeError("bad DDL"))
    pool = _pool(conn)

    settings = MagicMock()
    settings.storage.db_path = tmp_path / "db" / "trade.db"
    settings.storage.model_dir = tmp_path / "models"
    settings.storage.log_dir = tmp_path / "logs"

    with (
        patch.object(ts_mod, "get_settings", return_value=settings),
        patch.object(ts_mod.asyncpg, "create_pool", AsyncMock(return_value=pool)),
    ):
        with pytest.raises(RuntimeError, match="bad DDL"):
            await backend.initialize()

    # The half-built pool must not be leaked, and must not be published.
    pool.close.assert_awaited_once()
    assert backend._pool is None


async def test_run_migrations_noop_when_already_current(tmp_path):
    backend = _backend(tmp_path)
    conn = _conn()
    conn.fetchval = AsyncMock(return_value=_PG_SCHEMA_VERSION)
    await backend._run_migrations(conn)
    conn.execute.assert_not_awaited()


async def test_run_migrations_refuses_a_newer_database(tmp_path):
    backend = _backend(tmp_path)
    conn = _conn()
    conn.fetchval = AsyncMock(return_value=_PG_SCHEMA_VERSION + 1)
    with pytest.raises(RuntimeError, match="AHEAD of code version"):
        await backend._run_migrations(conn)


async def test_run_migrations_applies_only_pending_versions(tmp_path):
    backend = _backend(tmp_path)
    conn = _conn()
    # Database is one version behind -> only the final migration should apply.
    conn.fetchval = AsyncMock(return_value=_PG_SCHEMA_VERSION - 1)
    await backend._run_migrations(conn)

    inserts = [
        c.args for c in conn.execute.await_args_list if "INSERT INTO schema_version" in c.args[0]
    ]
    assert len(inserts) == 1
    assert inserts[0][1] == _PG_SCHEMA_VERSION


async def test_run_migrations_from_empty_applies_all(tmp_path):
    backend = _backend(tmp_path)
    conn = _conn()
    conn.fetchval = AsyncMock(return_value=0)
    await backend._run_migrations(conn)

    inserts = [
        c.args for c in conn.execute.await_args_list if "INSERT INTO schema_version" in c.args[0]
    ]
    assert len(inserts) == _PG_SCHEMA_VERSION
    assert [i[1] for i in inserts] == list(range(1, _PG_SCHEMA_VERSION + 1))
