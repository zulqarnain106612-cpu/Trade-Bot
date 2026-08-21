"""
Data quality gate — validates all incoming data before engine consumption.

All rejections log to audit_trail.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class QualityResult:
    passed: bool
    reason: str = ""


class DataQualityGate:
    """Validates data feeds before they reach the engine layer."""

    # ------------------------------------------------------------------
    # OHLCV
    # ------------------------------------------------------------------

    def check_ohlcv(self, df: pd.DataFrame) -> QualityResult:
        if df.empty:
            return QualityResult(False, "empty_dataframe")
        last_ts = pd.to_datetime(df["timestamp_utc"].iloc[-1], utc=True)
        age = datetime.now(UTC) - last_ts
        if age > timedelta(minutes=5):
            return self._reject(f"ohlcv_stale: {age.total_seconds():.0f}s old")
        returns = df["close"].pct_change().dropna()
        if (returns.abs() > 0.15).any():
            return self._reject("ohlcv_extreme_return: |ret| > 15%")
        if (df["volume"] == 0).rolling(3).sum().max() >= 3:
            return self._reject("ohlcv_zero_volume: 3 consecutive zero-volume candles")
        return QualityResult(True)

    # ------------------------------------------------------------------
    # Orderbook
    # ------------------------------------------------------------------

    def check_orderbook(self, spread_bps: float) -> QualityResult:
        if spread_bps > 200:
            return self._reject(f"orderbook_wide_spread: {spread_bps:.1f} bps")
        return QualityResult(True)

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    def check_options_row(self, iv: float, oi: float) -> QualityResult:
        if iv == 0.0:
            return self._reject("options_zero_iv")
        if oi == 0.0:
            return self._reject("options_zero_oi")
        return QualityResult(True)

    # ------------------------------------------------------------------
    # Macro (stale up to 2 days — weekends)
    # ------------------------------------------------------------------

    def check_macro(self, row: dict[str, Any]) -> QualityResult:
        date_str = row.get("date", "")
        if not date_str:
            return self._reject("macro_missing_date")
        try:
            date = datetime.fromisoformat(str(date_str)).replace(tzinfo=UTC)
        except ValueError:
            return self._reject("macro_bad_date_format")
        age = datetime.now(UTC) - date
        if age > timedelta(days=2):
            return self._reject(f"macro_stale: {age.days} days old")
        return QualityResult(True)

    # ------------------------------------------------------------------
    # Cross-validation: Binance mid vs secondary source
    # ------------------------------------------------------------------

    def check_price_deviation(self, primary_mid: float, secondary_mid: float) -> QualityResult:
        if primary_mid <= 0 or secondary_mid <= 0:
            return QualityResult(True)  # can't validate without both
        dev = abs(primary_mid - secondary_mid) / primary_mid
        if dev > 0.005:
            return self._reject(f"cross_source_deviation: {dev * 100:.2f}% vs secondary")
        return QualityResult(True)

    # ------------------------------------------------------------------
    # Sentiment
    # ------------------------------------------------------------------

    def check_sentiment_score(self, fg_score: float) -> QualityResult:
        if not (0.0 <= fg_score <= 100.0):
            return self._reject(f"sentiment_out_of_range: {fg_score}")
        return QualityResult(True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _reject(self, reason: str) -> QualityResult:
        log.warning("data_quality_reject", reason=reason)
        self._try_audit(reason)
        return QualityResult(False, reason)

    @staticmethod
    def _try_audit(reason: str) -> None:
        try:
            from src.diagnostics.audit_trail import get_audit_trail

            get_audit_trail().record(
                event_type="data_quality_reject",
                reason_code=reason[:120],
            )
        except Exception as exc:
            # Audit trail unavailable — must not block data validation, but the
            # rejection still needs to be visible somewhere.
            log.warning("audit_trail_record_failed", reason=reason[:120], exc=str(exc))
