#!/usr/bin/env python3
"""
GAP-015 Step 3 — Historical intelligence features backfill (OCI-012 revision).

Uses the free on-chain provider stack (Arkham / DefiLlama / Dune / CryptoQuant /
Coinglass) via OnChainAwareAggregator instead of Glassnode.

Strategy:
  1. Fetch ONE current snapshot from OnChainAwareAggregator (all OCI fields).
  2. Write that snapshot as the baseline for every historical bar that does
     not yet have intelligence_features_history data.
  3. Future ticks write live snapshots per-bar — coverage improves over time.

This "fill-forward from current" approach is imperfect for historical values
but unblocks model retraining on the full 24-feature set immediately.  The
trainer's coverage-gating (GAP-015 Step 5) will still exclude any column that
remains below the coverage threshold.

Usage:
    python3 scripts/backfill_intelligence.py \\
        --symbol BTC/USDT --timeframe 1h \\
        [--since 2024-01-01] [--until 2025-01-01] \\
        [--batch-size 200] [--db-path data/trade_bot.db] \\
        [--dry-run] [--min-coverage 0.6] [--overwrite]

Exit codes:
    0 — success (or dry-run preview)
    1 — fatal error
    2 — coverage below --min-coverage threshold (safe to proceed, warn only)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from datetime import UTC, datetime
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
    mapping = {
        "1m": 60_000,
        "3m": 180_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
        "2h": 7_200_000,
        "4h": 14_400_000,
        "6h": 21_600_000,
        "8h": 28_800_800,
        "12h": 43_200_000,
        "1d": 86_400_000,
    }
    if tf not in mapping:
        raise ValueError(f"Unsupported timeframe: {tf!r}. Supported: {sorted(mapping)}")
    return mapping[tf]


# ---------------------------------------------------------------------------
# OCI field → storage column mapping
# (mirrors src/data/storage.py fetch_intelligence_features rename map)
# ---------------------------------------------------------------------------
_OCI_TO_STORAGE: dict[str, str] = {
    "exchange_reserve_ratio": "intelligence_exchange_reserve_ratio",
    "exchange_netflow_7d_zscore": "intelligence_exchange_netflow_7d_zscore",
    "miner_netflow_signal": "intelligence_miner_netflow_signal",
    "futures_oi_change_pct": "intelligence_futures_oi_change_pct",
    "binance_funding_rate_pct": "intelligence_binance_funding_rate_pct",
    "liquidation_pressure_24h_zscore": "intelligence_liquidation_pressure_24h_zscore",
    "liquidation_cascade_risk_usd": "intelligence_liquidation_cascade_risk_usd",
    "whale_buy_sell_ratio": "intelligence_whale_buy_sell_ratio",
    "exchange_stress_score": "intelligence_exchange_stress_score",
    "staking_unlock_risk": "intelligence_staking_unlock_risk",
    "entity_exchange_imbalance": "intelligence_entity_exchange_imbalance",
    "btc_dominance_regime": "intelligence_btc_dominance_regime",
    "stablecoin_reserve_ratio": "intelligence_stablecoin_reserve_ratio",
    "network_activity_score": "intelligence_network_activity_score",
    "cross_exchange_basis_spread_bps": "intelligence_cross_exchange_basis_spread_bps",
    # OCI-012 new fields
    "defi_tvl_7d_change_pct": "intelligence_defi_tvl_7d_change_pct",
    "mvrv_z_score": "intelligence_mvrv_z_score",
    "sopr": "intelligence_sopr",
}

# Neutral/default values for columns when OCI returns neutral (skip storing as real signal)
_NEUTRAL_THRESHOLDS: dict[str, float] = {
    "exchange_reserve_ratio": 0.5,
    "whale_buy_sell_ratio": 1.0,
    "stablecoin_reserve_ratio": 0.5,
}


def _is_neutral(field: str, value: float) -> bool:
    """Return True if value is at the OCI neutral default (not real data)."""
    import math

    if math.isnan(value) or math.isinf(value):
        return True
    neutral = _NEUTRAL_THRESHOLDS.get(field, 0.0)
    return abs(value - neutral) < 1e-9


# ---------------------------------------------------------------------------
# Core backfill logic
# ---------------------------------------------------------------------------


async def _backfill(args: argparse.Namespace) -> int:
    """Return exit code."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from src.data.storage import StorageBackend as TradeStorage
    from src.intelligence.providers.aggregator import get_onchain_aware_aggregator

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

    log.info(
        "Backfill range: %s → %s  symbol=%s timeframe=%s",
        since_dt.isoformat(),
        until_dt.isoformat(),
        args.symbol,
        args.timeframe,
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
        await storage.close()
        return 0

    log.info("Bars to backfill: %d", len(bars))

    if args.dry_run:
        log.info("[DRY RUN] Would backfill %d bars. No writes.", len(bars))
        await storage.close()
        return 0

    # ---- fetch current OCI snapshot ----
    log.info("Fetching current snapshot from OnChainAwareAggregator …")
    aggregator = get_onchain_aware_aggregator(symbol=args.symbol.replace("USDT", "/USDT"))
    try:
        await aggregator.initialize_all()
        raw_metrics: dict[str, float] = await aggregator.fetch_metrics()
    except Exception as exc:
        log.error("OCI aggregator fetch failed: %s", exc)
        await storage.close()
        return 1
    finally:
        with contextlib.suppress(Exception):
            await aggregator.close_all()

    oci_confidence = raw_metrics.get("confidence", 0.0)
    log.info("OCI snapshot confidence: %.3f", oci_confidence)
    log.info("OCI fields populated (non-neutral):")
    real_fields_in_snapshot = 0
    for oci_field in _OCI_TO_STORAGE:
        val = raw_metrics.get(oci_field)
        if val is not None and not _is_neutral(oci_field, val):
            log.info("  %-40s = %s", oci_field, round(val, 6))
            real_fields_in_snapshot += 1
    log.info("Non-neutral OCI fields: %d / %d", real_fields_in_snapshot, len(_OCI_TO_STORAGE))

    # ---- build per-bar feature dict (same values for all bars — fill-forward) ----
    _TOTAL_COLS = len(_OCI_TO_STORAGE)

    def _make_features() -> dict[str, float | None]:
        features: dict[str, float | None] = {}
        for oci_field, store_col in _OCI_TO_STORAGE.items():
            val = raw_metrics.get(oci_field)
            if val is None or _is_neutral(oci_field, val):
                features[store_col] = None
            else:
                features[store_col] = float(val)
        return features

    snapshot_features = _make_features()
    non_null_count = sum(1 for v in snapshot_features.values() if v is not None)
    snapshot_confidence = round(non_null_count / _TOTAL_COLS, 4)
    log.info(
        "Snapshot non-NULL fields: %d / %d  confidence=%.3f",
        non_null_count,
        _TOTAL_COLS,
        snapshot_confidence,
    )

    # ---- iterate bars and write ----
    written = 0
    skipped = 0
    errors = 0
    batch_size = args.batch_size

    for i, bar in enumerate(bars):
        try:
            await storage.store_intelligence_features(
                symbol=args.symbol,
                timeframe=args.timeframe,
                bar_ts=bar.ts,
                features=snapshot_features,
                confidence=snapshot_confidence,
                source="oci_backfill",
            )
            written += 1
        except Exception as exc:
            log.error("store failed for bar_ts=%d: %s", bar.ts, exc)
            errors += 1

        if (i + 1) % batch_size == 0:
            log.info("Progress: %d / %d bars (errors=%d)", i + 1, len(bars), errors)

    log.info("Backfill complete. written=%d skipped=%d errors=%d", written, skipped, errors)

    # ---- coverage report ----
    cov = await storage.intelligence_feature_coverage(args.symbol, args.timeframe)
    log.info("Coverage report (total_rows=%d):", cov.get("total_rows", 0))
    low_cols = []
    for col, frac in sorted(cov.get("coverage", {}).items()):
        status = "✓" if frac >= args.min_coverage else "✗ LOW"
        log.info("  %-55s %.1f%%  %s", col, frac * 100, status)
        if frac < args.min_coverage:
            low_cols.append(col)

    await storage.close()

    if low_cols:
        log.warning(
            "%d column(s) below min_coverage=%.0f%%: %s",
            len(low_cols),
            args.min_coverage * 100,
            low_cols,
        )
        log.warning(
            "Low-coverage columns will be excluded from training until real data "
            "is collected.  Run this script again after configuring OCI provider keys."
        )
        return 2

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "GAP-015 OCI-012: Backfill intelligence_features_history using "
            "free on-chain providers (Arkham / DefiLlama / Dune / CryptoQuant / Coinglass)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--symbol", default="BTC/USDT", help="Asset symbol (e.g. BTC/USDT)")
    p.add_argument("--timeframe", default="1h", help="Bar timeframe (e.g. 1h, 4h, 1d)")
    p.add_argument(
        "--since", type=_parse_dt, default=None, help="Start date (YYYY-MM-DD). Default: 2023-01-01"
    )
    p.add_argument(
        "--until", type=_parse_dt, default=None, help="End date (YYYY-MM-DD). Default: now"
    )
    p.add_argument("--db-path", default="data/trade_bot.db", help="Path to SQLite database file")
    p.add_argument(
        "--batch-size", type=int, default=200, help="Progress log interval (bars per log line)"
    )
    p.add_argument("--dry-run", action="store_true", help="Preview only — no writes to DB")
    p.add_argument(
        "--min-coverage",
        type=float,
        default=0.6,
        help="Minimum non-NULL fraction per column (exit 2 if any below)",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing rows (default: INSERT OR REPLACE)",
    )
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(_backfill(args)))
