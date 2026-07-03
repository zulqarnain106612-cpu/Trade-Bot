#!/usr/bin/env python3
"""
GAP-015 Step 3 — Historical intelligence features backfill.

Walks every bar timestamp in storage for a given symbol/timeframe and
fetches the matching intelligence features from Glassnode + Binance,
storing results into intelligence_features_history via the migration-v3 table.

Only Binance funding rate (public, no key) will populate until
GLASSNODE_API_KEY is provisioned.  The script is designed to be re-run
idempotently (INSERT OR REPLACE) — previously fetched rows are overwritten
with fresh data.

Usage:
    python3 scripts/backfill_intelligence.py \\
        --symbol BTCUSDT --timeframe 1h \\
        [--since 2024-01-01] [--until 2025-01-01] \\
        [--batch-size 200] [--db-path data/trade_bot.db] \\
        [--dry-run] [--min-coverage 0.6]

Exit codes:
    0 — success (or dry-run preview)
    1 — fatal error
    2 — coverage below --min-coverage threshold (safe to proceed, but flag)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("backfill_intelligence")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(s: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Cannot parse date: {s!r}")


def _timeframe_to_ms(tf: str) -> int:
    """Convert timeframe string to milliseconds per bar."""
    mapping = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
        "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000,
        "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000,
        "12h": 43_200_000, "1d": 86_400_000,
    }
    if tf not in mapping:
        raise ValueError(f"Unsupported timeframe: {tf!r}. Supported: {sorted(mapping)}")
    return mapping[tf]


# ---------------------------------------------------------------------------
# Core backfill logic
# ---------------------------------------------------------------------------

async def _backfill(args: argparse.Namespace) -> int:
    """Return exit code."""
    # -- import here so script works from project root without install --
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from src.data.storage import TradeStorage
    from src.intelligence.client import IntelligenceAggregator

    # ---- open DB ----
    db_path = Path(args.db_path)
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 1

    storage = TradeStorage(str(db_path))
    await storage.initialize()

    # ---- resolve time range ----
    since_dt: datetime = args.since or datetime(2023, 1, 1, tzinfo=UTC)
    until_dt: datetime = args.until or datetime.now(UTC)
    since_ms = int(since_dt.timestamp() * 1000)
    until_ms = int(until_dt.timestamp() * 1000)
    tf_ms = _timeframe_to_ms(args.timeframe)

    log.info(
        "Backfill range: %s → %s  symbol=%s timeframe=%s",
        since_dt.isoformat(), until_dt.isoformat(), args.symbol, args.timeframe,
    )

    # ---- fetch bars in range ----
    bars = await storage.fetch_bars(
        symbol=args.symbol,
        timeframe=args.timeframe,
        since_ts=since_ms,
        limit=500_000,
    )
    bars = [b for b in bars if b.ts <= until_ms]
    if not bars:
        log.warning("No bars found in range — nothing to backfill.")
        return 0
    log.info("Bars to backfill: %d", len(bars))

    if args.dry_run:
        log.info("[DRY RUN] Would backfill %d bars. No writes.", len(bars))
        return 0

    # ---- build intelligence client ----
    aggregator = IntelligenceAggregator()
    has_glassnode = bool(aggregator.glassnode_key)
    if not has_glassnode:
        log.warning(
            "GLASSNODE_API_KEY not set — only Binance funding rate will be populated. "
            "Set INTELLIGENCE_GLASSNODE_API_KEY in .env and re-run for full coverage."
        )

    # ---- pre-fetch Binance funding rate history (public, bulk) ----
    log.info("Pre-fetching Binance funding rate history …")
    funding_history = await aggregator.get_funding_rate_history(
        since_ts=since_ms,
        limit=1000,
    )
    # Build lookup: bar_ts_ms (aligned to 8h funding period) → rate_pct
    # Binance funding rate timestamps are in ms.  We map each funding period
    # to all bars that fall within that 8h window.
    funding_map: dict[int, float] = {}
    for entry in funding_history:
        funding_map[int(entry["ts"])] = float(entry["rate_pct"])
    log.info("Funding rate entries fetched: %d", len(funding_map))

    def _nearest_funding_rate(bar_ts_ms: int) -> float | None:
        """Return rate for the most-recent funding period at or before bar_ts."""
        candidates = [ts for ts in funding_map if ts <= bar_ts_ms]
        if not candidates:
            return None
        return funding_map[max(candidates)]

    # ---- pre-fetch Glassnode history (if key present) ----
    netflow_map: dict[int, dict] = {}
    whale_map: dict[int, dict] = {}
    if has_glassnode:
        log.info("Fetching Glassnode exchange netflow history …")
        netflow_series = await aggregator.get_exchange_netflow_history(
            symbol="BTC",
            since_ts=int(since_dt.timestamp()),
            until_ts=int(until_dt.timestamp()),
            interval="24h",
        )
        # Glassnode returns daily buckets; map each to its Unix-ms timestamp
        for row in netflow_series:
            netflow_map[row["ts"] * 1000] = row  # ts is Unix seconds

        log.info("Glassnode netflow entries: %d", len(netflow_map))

        log.info("Fetching Glassnode whale activity history …")
        whale_series = await aggregator.get_whale_activity_history(
            symbol="BTC",
            since_ts=int(since_dt.timestamp()),
            until_ts=int(until_dt.timestamp()),
            interval="24h",
        )
        for row in whale_series:
            whale_map[row["ts"] * 1000] = row

        log.info("Glassnode whale entries: %d", len(whale_map))

    def _nearest_daily(lookup: dict[int, dict], bar_ts_ms: int) -> dict | None:
        """Return the most-recent daily entry at or before bar_ts_ms."""
        candidates = [ts for ts in lookup if ts <= bar_ts_ms]
        if not candidates:
            return None
        return lookup[max(candidates)]

    # ---- iterate bars and write ----
    written = 0
    errors = 0
    batch_size = args.batch_size

    for i, bar in enumerate(bars):
        features: dict[str, float | None] = {}
        real_fields = 0

        # Funding rate (always attempted — public Binance API)
        fr = _nearest_funding_rate(bar.ts)
        features["intelligence_binance_funding_rate_pct"] = fr
        if fr is not None:
            real_fields += 1

        # Glassnode netflow
        if has_glassnode:
            nf = _nearest_daily(netflow_map, bar.ts)
            if nf:
                features["intelligence_exchange_netflow_7d_zscore"] = nf.get("tscore")
                real_fields += 1
            wh = _nearest_daily(whale_map, bar.ts)
            if wh:
                features["intelligence_whale_buy_sell_ratio"] = wh.get("ratio")
                real_fields += 1

        # Fields not yet fetchable (CryptoQuant key absent, or endpoint not built)
        # Stored as NULL — trainer coverage check will detect and drop if needed.
        for col in [
            "intelligence_exchange_reserve_ratio",
            "intelligence_miner_netflow_signal",
            "intelligence_staking_unlock_risk",
            "intelligence_entity_exchange_imbalance",
            "intelligence_liquidation_pressure_24h_zscore",
            "intelligence_futures_oi_change_pct",
            "intelligence_liquidation_cascade_risk_usd",
            "intelligence_btc_dominance_regime",
            "intelligence_stablecoin_reserve_ratio",
            "intelligence_network_activity_score",
            "intelligence_exchange_stress_score",
            "intelligence_cross_exchange_basis_spread_bps",
        ]:
            features.setdefault(col, None)

        # Confidence = fraction of the 15 fields that are non-NULL
        _TOTAL = 15
        non_null = sum(1 for v in features.values() if v is not None)
        confidence = round(non_null / _TOTAL, 4)

        try:
            await storage.store_intelligence_features(
                symbol=args.symbol,
                timeframe=args.timeframe,
                bar_ts=bar.ts,
                features=features,
                confidence=confidence,
                source="backfill",
            )
            written += 1
        except Exception as exc:
            log.error("store failed for bar_ts=%d: %s", bar.ts, exc)
            errors += 1

        if (i + 1) % batch_size == 0:
            log.info("Progress: %d / %d bars written (errors=%d)", i + 1, len(bars), errors)

    log.info("Backfill complete. written=%d errors=%d", written, errors)

    # ---- coverage report ----
    cov = await storage.intelligence_feature_coverage(args.symbol, args.timeframe)
    log.info("Coverage report (total_rows=%d):", cov.get("total_rows", 0))
    low_cols = []
    for col, frac in sorted(cov.get("coverage", {}).items()):
        status = "✓" if frac >= args.min_coverage else "✗ LOW"
        log.info("  %s  %.1f%%  %s", col, frac * 100, status)
        if frac < args.min_coverage:
            low_cols.append(col)

    if low_cols:
        log.warning(
            "%d column(s) below min_coverage=%.0f%%: %s",
            len(low_cols), args.min_coverage * 100, low_cols,
        )
        log.warning(
            "These columns will be excluded from training until provisioned. "
            "Re-run after adding GLASSNODE_API_KEY / CRYPTOQUANT_API_KEY."
        )
        return 2

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="GAP-015: Backfill intelligence_features_history from Glassnode + Binance.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--symbol", default="BTCUSDT", help="Asset symbol")
    p.add_argument("--timeframe", default="1h", help="Bar timeframe (e.g. 1h, 4h, 1d)")
    p.add_argument("--since", type=_parse_dt, default=None,
                   help="Start date (YYYY-MM-DD). Default: 2023-01-01")
    p.add_argument("--until", type=_parse_dt, default=None,
                   help="End date (YYYY-MM-DD). Default: now")
    p.add_argument("--db-path", default="data/trade_bot.db",
                   help="Path to SQLite database file")
    p.add_argument("--batch-size", type=int, default=200,
                   help="Progress log interval (bars per log line)")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview only — no writes to DB")
    p.add_argument("--min-coverage", type=float, default=0.6,
                   help="Minimum non-NULL fraction per column (exit 2 if any below)")
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(_backfill(args)))
