"""
GAP-006: one-shot data migration — SQLite -> local TimescaleDB.

Copies every row of every shared table from the SQLite file into the
TimescaleDB container. Idempotent: every insert is ON CONFLICT DO NOTHING,
so the script can be re-run safely (e.g. after a partial failure).

Prerequisites:
    bash scripts/timescaledb.sh up

Usage:
    uv run python scripts/migrate_sqlite_to_timescale.py               # defaults
    uv run python scripts/migrate_sqlite_to_timescale.py --sqlite data/trade_bot.db
    uv run python scripts/migrate_sqlite_to_timescale.py --dry-run    # counts only

Column handling is introspected, not hardcoded: for each table the script
inserts the intersection of (source sqlite columns, target postgres columns),
excluding target identity columns (their values are regenerated). This keeps
the script correct as schema migrations evolve on either side.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import aiosqlite
import asyncpg


# Allow running as a plain script from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_settings
from src.data.timescale_storage import TimescaleBackend


# Tables in dependency-free copy order (no FKs between them).
_TABLES = [
    "bars",
    "trades",
    "regime_snapshots",
    "model_metrics",
    "equity_curve",
    "audit_log",
    "intelligence_features_history",
]

_BATCH = 5_000


async def _target_columns(conn: asyncpg.Connection, table: str) -> list[str]:
    """Non-identity column names of a target table (identity ids regenerate)."""
    rows = await conn.fetch(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = $1 AND is_identity = 'NO'
        ORDER BY ordinal_position
        """,
        table,
    )
    return [r["column_name"] for r in rows]


async def _source_columns(conn: aiosqlite.Connection, table: str) -> list[str]:
    cur = await conn.execute(f"PRAGMA table_info({table})")  # nosec B608  # nosemgrep: no-fstring-sql — table from _TABLES literal
    rows = await cur.fetchall()
    return [r[1] for r in rows]


async def _copy_table(
    sqlite_conn: aiosqlite.Connection,
    pool: asyncpg.Pool,
    table: str,
    dry_run: bool,
) -> tuple[int, int]:
    """Copy one table; returns (source_rows, inserted_rows)."""
    async with pool.acquire() as pg:
        tgt_cols = await _target_columns(pg, table)
    src_cols = await _source_columns(sqlite_conn, table)
    cols = [c for c in src_cols if c in tgt_cols]
    if not cols:
        print(f"  {table}: no shared columns — skipped")
        return 0, 0

    col_list = ", ".join(cols)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    insert_sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "  # nosec B608 — identifiers from information_schema/PRAGMA, not user input
        "ON CONFLICT DO NOTHING"
    )

    cur = await sqlite_conn.execute(f"SELECT {col_list} FROM {table}")  # nosec B608  # nosemgrep: no-fstring-sql
    total = 0
    inserted = 0
    while True:
        rows = await cur.fetchmany(_BATCH)
        if not rows:
            break
        total += len(rows)
        if dry_run:
            continue
        batch = [tuple(row) for row in rows]
        async with pool.acquire() as pg:
            # executemany runs in one implicit transaction per call
            before = await pg.fetchval(f"SELECT COUNT(*) FROM {table}")  # nosec B608
            await pg.executemany(insert_sql, batch)
            after = await pg.fetchval(f"SELECT COUNT(*) FROM {table}")  # nosec B608
            inserted += after - before
    return total, inserted


async def migrate(sqlite_path: str, dsn: str, dry_run: bool) -> int:
    if not Path(sqlite_path).exists():
        print(f"ERROR: sqlite db not found: {sqlite_path}")
        return 1

    # Let the backend create/upgrade the target schema first.
    backend = TimescaleBackend(dsn=dsn)
    await backend.initialize()
    pool = backend._pool  # — migration tooling, same package family
    assert pool is not None

    sqlite_conn = await aiosqlite.connect(sqlite_path)
    try:
        print(f"migrating {sqlite_path} -> {dsn.split('@')[-1]}  (dry_run={dry_run})")
        grand_src = grand_ins = 0
        for table in _TABLES:
            src, ins = await _copy_table(sqlite_conn, pool, table, dry_run)
            grand_src += src
            grand_ins += ins
            print(f"  {table}: {src} source rows -> {ins} inserted")
        print(f"done: {grand_src} source rows, {grand_ins} newly inserted")
        return 0
    finally:
        await sqlite_conn.close()
        await backend.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate SQLite data to TimescaleDB (GAP-006)")
    parser.add_argument(
        "--sqlite",
        default=str(get_settings().storage.db_path),
        help="source SQLite file (default: STORAGE_DB_PATH)",
    )
    parser.add_argument(
        "--dsn",
        default=get_settings().storage.timescale_dsn,
        help="target DSN (default: STORAGE_TIMESCALE_DSN)",
    )
    parser.add_argument("--dry-run", action="store_true", help="count rows only, write nothing")
    args = parser.parse_args()
    return asyncio.run(migrate(args.sqlite, args.dsn, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
