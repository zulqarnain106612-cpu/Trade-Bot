"""
Dune Analytics provider — OCI-004.

CACHE-FIRST design: never execute a query unless results are stale.
Budget: ≤5 executions/day (free tier constraint).

Populates:
  miner_netflow_signal  — z-score of miner outflow (bearish pressure)
  mvrv_z_score          — NEW, gated until OCI-007 schema
  sopr                  — NEW, gated until OCI-007 schema

Auth: X-Dune-API-Key header.

Authority: https://docs.dune.com/api-reference/executions/endpoint/execute-query
"""

from __future__ import annotations

import asyncio
import math
import time

import structlog

from src.intelligence.onchain.base import OnChainProvider


log = structlog.get_logger(__name__)

_BASE = "https://api.dune.com/api/v1"

# Public community query IDs — update constants here if IDs change, no other edits needed.
DUNE_QUERY_MINER_OUTFLOW = 2732847
DUNE_QUERY_MVRV_ZSCORE = 3237234
DUNE_QUERY_SOPR = 2691043

_MAX_DAILY_EXECUTIONS = 5
_POLL_TIMEOUT_S = 5.0
_POLL_INTERVAL_S = 1.0
_EPS = 1e-9

_NEUTRAL: dict[str, float] = {
    "miner_netflow_signal": 0.0,
    "mvrv_z_score": 0.0,
    "sopr": 0.0,
}
_CONFIDENCE_PENALTY = 0.05


class DuneProvider(OnChainProvider):
    """
    Dune Analytics on-chain provider.

    Requires INTELLIGENCE_DUNE_API_KEY in env / config.
    Cache-first: checks result freshness before executing a query.
    Daily execution budget enforced via in-memory counter (resets at UTC midnight).
    """

    _BASE_URL = _BASE
    _CACHE_TTL_S = 3600
    _RATE = 1.0  # conservative; Dune free tier throttles aggressively

    def __init__(self, api_key: str, cache_ttl_s: int = 3600) -> None:
        super().__init__()
        self._api_key = api_key
        self._CACHE_TTL_S = cache_ttl_s
        self._daily_executions = 0
        self._exec_day: int = _today_ordinal()

    @property
    def exchange_id(self) -> str:
        return "dune_analytics"

    def _auth(self) -> dict[str, str]:
        return {"X-Dune-API-Key": self._api_key, "Content-Type": "application/json"}

    def _budget_remaining(self) -> bool:
        today = _today_ordinal()
        if today != self._exec_day:
            self._daily_executions = 0
            self._exec_day = today
        return self._daily_executions < _MAX_DAILY_EXECUTIONS

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        await super().close()

    # ------------------------------------------------------------------
    # fetch_metrics
    # ------------------------------------------------------------------

    async def fetch_metrics(self) -> dict[str, float]:
        result = dict(_NEUTRAL)
        confidence = 1.0

        if not self._api_key:
            result["confidence"] = 0.0
            result["timestamp"] = int(time.time())
            return result

        # 1. miner_netflow_signal
        rows = await self._get_query_rows(DUNE_QUERY_MINER_OUTFLOW)
        if rows is not None:
            result["miner_netflow_signal"] = _miner_netflow_zscore(rows)
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 2. mvrv_z_score (new field — gated until OCI-007)
        rows2 = await self._get_query_rows(DUNE_QUERY_MVRV_ZSCORE)
        if rows2 is not None and rows2:
            latest = rows2[-1]
            result["mvrv_z_score"] = float(latest.get("mvrv_z", 0) or 0)
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 3. sopr (new field — gated until OCI-007)
        rows3 = await self._get_query_rows(DUNE_QUERY_SOPR)
        if rows3 is not None and rows3:
            sopr_val = float(rows3[-1].get("sopr_7d_ma", 1.0) or 1.0)
            # Normalize: (sopr - 1) x 2, clamp to [-1, +1]
            result["sopr"] = max(-1.0, min(1.0, (sopr_val - 1.0) * 2.0))
        else:
            confidence -= _CONFIDENCE_PENALTY

        result["confidence"] = max(0.0, confidence)
        result["timestamp"] = int(time.time())
        return result

    async def _get_query_rows(self, query_id: int) -> list[dict] | None:
        """
        Cache-first fetch:
        1. GET /query/{id}/results — if state=SUCCESS and age < ttl → return rows
        2. If stale and budget → POST /query/{id}/execute, poll, return rows
        3. On timeout / budget exhausted → return cached rows or None
        """
        results = await self._get(
            f"{_BASE}/query/{query_id}/results",
            headers=self._auth(),
        )
        if results is not None and _results_fresh(results, self._CACHE_TTL_S):
            return _extract_rows(results)

        # Use stale cache if execution budget exhausted
        if not self._budget_remaining():
            log.warning("dune_budget_exhausted", query_id=query_id)
            return _extract_rows(results) if results is not None else None

        # Execute the query
        exec_resp = await self._post(
            f"{_BASE}/query/{query_id}/execute",
            headers=self._auth(),
            json={},
        )
        if exec_resp is None:
            return _extract_rows(results) if results is not None else None

        self._daily_executions += 1
        execution_id = exec_resp.get("execution_id")
        if not execution_id:
            return None

        # Poll until SUCCESS or timeout
        deadline = time.monotonic() + _POLL_TIMEOUT_S
        while time.monotonic() < deadline:
            status = await self._get(
                f"{_BASE}/execution/{execution_id}/results",
                headers=self._auth(),
            )
            if status is not None and status.get("state") == "QUERY_STATE_COMPLETED":
                return _extract_rows(status)
            await asyncio.sleep(_POLL_INTERVAL_S)

        log.warning("dune_poll_timeout", query_id=query_id)
        # Fall back to stale cache
        return _extract_rows(results) if results is not None else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_ordinal() -> int:
    import datetime

    return datetime.date.today().toordinal()


def _results_fresh(results: dict, ttl_s: int) -> bool:
    """True if result state is SUCCESS and the result was generated within ttl_s seconds."""
    if results.get("state") not in ("QUERY_STATE_COMPLETED", "SUCCESS"):
        return False
    executed_at = results.get("execution_ended_at") or results.get("submitted_at") or ""
    if not executed_at:
        return False
    try:
        import datetime

        ts = datetime.datetime.fromisoformat(executed_at.replace("Z", "+00:00"))
        age = (datetime.datetime.now(datetime.UTC) - ts).total_seconds()
        return age < ttl_s
    except Exception as exc:
        # Fail closed (treat as stale, forcing a refetch) but log so a
        # persistently malformed timestamp field is visible to operators
        # instead of silently masquerading as "not fresh" forever.
        log.warning("dune.results_fresh_check_failed", error=str(exc), exc_info=True)
        return False


def _extract_rows(results: dict | None) -> list[dict] | None:
    if results is None:
        return None
    rows = results.get("result", {}).get("rows") or results.get("rows") or []
    return rows if isinstance(rows, list) else None


def _miner_netflow_zscore(rows: list[dict]) -> float:
    """Z-score of latest miner outflow vs 90d mean; clamped to [-1, +1]."""
    values = [float(r.get("miner_outflow_btc_7d", 0) or 0) for r in rows]
    if not values:
        return 0.0
    if len(values) < 2:
        return 0.0
    latest = values[-1]
    window = values[-90:]
    mean = sum(window) / len(window)
    var = sum((x - mean) ** 2 for x in window) / len(window)
    std = math.sqrt(var) if var > 0 else _EPS
    return max(-1.0, min(1.0, (latest - mean) / std))
