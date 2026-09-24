#!/usr/bin/env python3
"""
Historical OHLCV backfill CLI.

Wraps ``src.data.fetcher.MarketDataFetcher.bootstrap_history`` so an operator
can prime the storage backend with N days of bars before starting the API /
worker for the first time. Runs standalone — no orchestrator, no engines.

Usage:

    uv run python scripts/backfill.py \\
        --symbol BTC/USDT \\
        --timeframes 5m,15m,1h \\
        --lookback-days 180

The default symbol and timeframes come from ``Settings.primary_symbol`` and
``Settings.active_timeframes`` (see src/config.py).

Exit codes:
    0  every requested (symbol, timeframe) wrote at least one bar
    1  one or more (symbol, timeframe) pairs wrote zero bars — check logs
    2  unrecoverable error (bad args, storage unreachable, exchange auth)
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog

from src.config import Timeframe, get_settings
from src.data.fetcher import open_fetcher
from src.data.storage import create_storage_backend
from src.logging_setup import configure_logging

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_VALID_TFS = {tf.value: tf for tf in Timeframe}


def _parse_timeframes(raw: str) -> list[Timeframe]:
    out: list[Timeframe] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if token not in _VALID_TFS:
            raise SystemExit(
                f"unknown timeframe {token!r}; valid: {sorted(_VALID_TFS)}"
            )
        out.append(_VALID_TFS[token])
    if not out:
        raise SystemExit("--timeframes must list at least one value")
    return out


async def _run(symbol: str, timeframes: list[Timeframe], lookback_days: int) -> int:
    storage = create_storage_backend()
    await storage.initialize()
    all_wrote_bars = True

    try:
        async with open_fetcher(storage) as fetcher:
            for tf in timeframes:
                written = await fetcher.bootstrap_history(
                    symbol=symbol,
                    timeframe=tf,
                    lookback_days=lookback_days,
                )
                log.info(
                    "backfill.timeframe_done",
                    symbol=symbol,
                    timeframe=tf.value,
                    bars_written=written,
                )
                if written == 0:
                    all_wrote_bars = False
    finally:
        await storage.close()

    return 0 if all_wrote_bars else 1


def main() -> int:
    settings = get_settings()
    configure_logging(settings)

    parser = argparse.ArgumentParser(description="Trade-Bot historical backfill")
    parser.add_argument(
        "--symbol",
        default=settings.primary_symbol,
        help="Trading pair (default: Settings.primary_symbol)",
    )
    parser.add_argument(
        "--timeframes",
        default=",".join(tf.value for tf in settings.active_timeframes),
        help="Comma-separated timeframes (default: Settings.active_timeframes)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=180,
        help="Days of history to fetch (default: 180)",
    )
    args = parser.parse_args()

    if args.lookback_days < 1:
        raise SystemExit("--lookback-days must be >= 1")

    tfs = _parse_timeframes(args.timeframes)

    try:
        return asyncio.run(_run(args.symbol, tfs, args.lookback_days))
    except KeyboardInterrupt:
        log.warning("backfill.interrupted")
        return 2
    except Exception:
        log.critical("backfill.failed", exc_info=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
