"""
Trade Auditor — captures every signal decision with full diagnostic context.

Every tick that reaches a trade decision (or rejection) is recorded with:
  - All feature values at decision time
  - Model probabilities (direction P(long), meta P(bet))
  - Regime state + probabilities
  - Gate evaluation chain (which gate fired first)
  - Kelly sizing inputs and output
  - Execution outcome (opened / queued / skipped / rejected)

This creates a queryable audit trail for post-hoc debugging.

Authority:
  - López de Prado (2018) AFML Ch.14 — strategy diagnostics and explainability
  - Aronson (2006) Evidence-Based Technical Analysis — decision logging
  - Carver (2019) Systematic Trading — trade journal as debugging tool
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC
from typing import Any, Final

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

MAX_AUDIT_RECORDS: Final[int] = 2000  # rolling window — FIFO eviction


# ---------------------------------------------------------------------------
# Audit record
# ---------------------------------------------------------------------------


@dataclass
class AuditRecord:
    """
    Complete decision snapshot for one signal engine tick.

    Frozen at decision time — never mutated after creation.
    """

    ts_utc: float  # Unix seconds
    symbol: str
    timeframe: str

    # Feature vector at decision time
    features: dict[str, float]

    # Model outputs
    p_long: float  # XGBoost direction P(long)
    p_bet: float  # Meta-label P(bet)
    direction: int  # 1=long, 0=short

    # Regime (Hamilton 1989)
    regime_state: int | None
    prob_ranging: float | None
    prob_trending: float | None
    prob_volatile: float | None

    # Risk gate
    gate_status: str  # GateStatus.value
    gate_reason: str
    gate_details: dict[str, Any]

    # Kelly sizing
    kelly_fraction: float | None
    kelly_notional_usd: float | None
    kelly_quantity: float | None
    kelly_is_capped: bool | None

    # Outcome
    outcome: str  # 'opened'|'queued'|'skipped'|'rejected'
    trade_id: str | None
    skip_reason: str

    # Diagnostics
    tick_latency_ms: float = 0.0  # wall-clock ms for full tick
    equity_usd_at_decision: float = 0.0

    # Ensemble blending (RiskSettings.ensemble_blend_weight) — None when the
    # ensemble predictor isn't wired in or blend weight is 0.0 for this tick.
    ensemble_point_estimate: float | None = None
    ensemble_uncertainty: float | None = None
    ensemble_blend_weight: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts_iso"] = _ts_to_iso(self.ts_utc)
        return d


def _ts_to_iso(ts: float) -> str:
    from datetime import datetime

    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------


class TradeAuditor:
    """
    Thread-safe rolling audit log for signal decisions.

    Backed by a deque(maxlen=MAX_AUDIT_RECORDS) — O(1) append/eviction.
    All writes happen on the event loop; no locks needed for reads from
    the same thread.
    """

    def __init__(self, max_records: int = MAX_AUDIT_RECORDS) -> None:
        self._records: deque[AuditRecord] = deque(maxlen=max_records)
        self._n_total = 0  # never resets — total ticks seen
        self._n_tradeable = 0
        self._n_skipped = 0

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #

    def record(self, rec: AuditRecord) -> None:
        """Append an audit record. Evicts oldest if at capacity."""
        self._records.append(rec)
        self._n_total += 1
        if rec.outcome == "opened":
            self._n_tradeable += 1
        else:
            self._n_skipped += 1

        # Structured log at DEBUG — queryable in log aggregators
        log.debug(
            "audit.decision",
            symbol=rec.symbol,
            tf=rec.timeframe,
            outcome=rec.outcome,
            p_long=round(rec.p_long, 4),
            p_bet=round(rec.p_bet, 4),
            gate=rec.gate_status,
            regime=rec.regime_state,
            kelly_notional=rec.kelly_notional_usd,
            skip=rec.skip_reason or None,
        )

    # ------------------------------------------------------------------ #
    # Query
    # ------------------------------------------------------------------ #

    def recent(self, n: int = 100) -> list[AuditRecord]:
        """Return the N most-recent records, newest first."""
        records = list(self._records)
        return list(reversed(records[-n:]))

    def summary(self) -> dict[str, Any]:
        """Aggregate stats for the /debug/audit endpoint."""
        records = list(self._records)
        if not records:
            return {
                "n_total": self._n_total,
                "n_tradeable": self._n_tradeable,
                "n_skipped": self._n_skipped,
                "trade_rate_pct": 0.0,
                "gate_breakdown": {},
                "skip_reason_breakdown": {},
                "regime_breakdown": {},
                "mean_p_long": None,
                "mean_p_bet": None,
            }

        gate_counts: dict[str, int] = {}
        skip_counts: dict[str, int] = {}
        regime_counts: dict[str, int] = {}
        p_long_sum = p_bet_sum = 0.0

        for r in records:
            gate_counts[r.gate_status] = gate_counts.get(r.gate_status, 0) + 1
            if r.skip_reason:
                skip_counts[r.skip_reason] = skip_counts.get(r.skip_reason, 0) + 1
            rk = str(r.regime_state) if r.regime_state is not None else "unknown"
            regime_counts[rk] = regime_counts.get(rk, 0) + 1
            p_long_sum += r.p_long
            p_bet_sum += r.p_bet

        n = len(records)
        return {
            "n_total": self._n_total,
            "n_tradeable": self._n_tradeable,
            "n_skipped": self._n_skipped,
            "trade_rate_pct": round(self._n_tradeable / max(self._n_total, 1) * 100, 2),
            "gate_breakdown": gate_counts,
            "skip_reason_breakdown": skip_counts,
            "regime_breakdown": regime_counts,
            "mean_p_long": round(p_long_sum / n, 4),
            "mean_p_bet": round(p_bet_sum / n, 4),
        }

    def anomaly_scan(self) -> list[str]:
        """
        Scan recent records for statistical anomalies.

        Checks (Tulchinsky 2019 — signal decay detection):
          - p_long distribution collapse → model may be degraded
          - p_bet consistently near 0.5 → meta-label not discriminating
          - Gate always firing same reason → systematic misconfiguration
          - Kelly fraction always capped → position sizing ceiling always binding

        Returns list of anomaly description strings (empty = no issues).
        """
        records = list(self._records)
        if len(records) < 50:
            return []

        alerts: list[str] = []
        recent = records[-200:]

        # 1. p_long variance collapse (Tulchinsky 2019 — alpha decay)
        import statistics

        try:
            plong_vals = [r.p_long for r in recent]
            plong_std = statistics.stdev(plong_vals)  # UI-012: was computed twice
            if plong_std < 0.02:
                alerts.append(
                    f"p_long_variance_collapsed: stdev={plong_std:.4f} "
                    f"— direction model may be degenerate (AFML Ch.16)"
                )
        except statistics.StatisticsError as exc:
            # UI-012: this anomaly check exists specifically to catch a
            # degenerate/constant model feed -- silently dropping it on
            # StatisticsError (e.g. <2 distinct values, which is the
            # degenerate case itself) hid exactly the failure it's meant
            # to detect.
            log.debug("trade_auditor.plong_variance_check_failed", error=str(exc))

        # 2. Meta-label stuck near 0.5 — no discrimination
        try:
            pbet_vals = [r.p_bet for r in recent]
            pbet_mean = statistics.mean(pbet_vals)
            pbet_std = statistics.stdev(pbet_vals)
            if abs(pbet_mean - 0.5) < 0.05 and pbet_std < 0.05:
                alerts.append(
                    f"meta_label_not_discriminating: mean={pbet_mean:.3f}, "
                    f"stdev={pbet_std:.4f} — retrain meta-label model (AFML Ch.4)"
                )
        except statistics.StatisticsError as exc:
            log.debug("trade_auditor.meta_label_check_failed", error=str(exc))

        # 3. Gate always firing same reason → misconfiguration
        gate_counts: dict[str, int] = {}
        for r in recent:
            gate_counts[r.gate_status] = gate_counts.get(r.gate_status, 0) + 1
        dominant_gate, dominant_count = max(gate_counts.items(), key=lambda x: x[1])
        if dominant_count / len(recent) > 0.90 and dominant_gate != "pass":
            alerts.append(
                f"gate_always_firing: {dominant_gate} on {dominant_count}/{len(recent)} ticks "
                f"— check RiskSettings configuration (López de Prado AFML Ch.3)"
            )

        # 4. Kelly always capped — ceiling always binding
        capped = [r for r in recent if r.kelly_is_capped is True]
        if len(capped) / max(len(recent), 1) > 0.80:
            alerts.append(
                f"kelly_ceiling_always_binding: {len(capped)}/{len(recent)} trades capped "
                f"— consider lowering kelly_ceiling or win-prob estimate (Kelly 1956)"
            )

        return alerts


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_auditor: TradeAuditor | None = None


def get_auditor() -> TradeAuditor:
    global _auditor
    if _auditor is None:
        _auditor = TradeAuditor()
    return _auditor
