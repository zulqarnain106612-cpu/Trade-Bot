"""
FastAPI dashboard API.

Security: ALL endpoints require X-API-Key header matching API_SECRET_KEY env var.

Endpoints:
  GET  /health                     — system health + storage counts
  GET  /status                     — equity, positions, regime, execution mode
  GET  /trades                     — paginated trade history
  GET  /equity                     — equity curve for charting
  GET  /regime/{timeframe}         — latest regime snapshot
  GET  /approvals                  — pending approval requests
  POST /approvals/{id}/resolve     — approve or reject a pending trade
  POST /execution-mode             — switch AUTOMATIC/RESTRICTED/MANUAL at runtime
  WS   /ws                         — live push of equity + positions + signals

WebSocket push format (JSON):
  { "type": "tick", "equity_usd": ..., "positions": [...], "regime": {...} }
  { "type": "approval", "request": {...} }
  { "type": "trade", "trade": {...} }
"""

from __future__ import annotations

import asyncio
import collections
import hmac  # SCAN3-003: moved from inline import inside set_execution_mode()
import json
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import structlog
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from src.api.auth import verify_api_key, verify_ws_key
from src.api.metrics import metrics_output
from src.api.middleware import validate_cors_config
from src.config import ExecutionMode, Timeframe, get_settings, runtime_config
from src.data.fetcher import open_fetcher
from src.data.storage import AnyStorageBackend, create_storage_backend
from src.engine.orchestrator import Orchestrator
from src.execution.base import AbstractExecutor
from src.tuning.audit import TuningEventType
from src.tuning.scheduler import AutoTuningScheduler
from src.tuning.state import (
    audit_log as tuning_audit_log,
    parameter_registry as tuning_registry,
    pause_state as tuning_pause_state,
    version_store as tuning_version_store,
    watchdog as tuning_watchdog,
)
from src.tuning.store import NoPriorVersionError, NoVersionsError


# H-13: UUID format regex — prevents timing oracle via huge string hash and DoS
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Operator field validator — reused across request models
# ---------------------------------------------------------------------------

_OPERATOR_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def _validate_operator(v: str) -> str:
    if not _OPERATOR_RE.match(v):
        raise ValueError("operator must be 1-64 alphanumeric/underscore/hyphen characters")
    return v


# ---------------------------------------------------------------------------
# Application state — shared across requests
# ---------------------------------------------------------------------------


class AppState:
    storage: AnyStorageBackend
    orchestrator: Orchestrator
    ready: bool  # True only after orchestrator.startup() completes
    _MAX_WS_CLIENTS: int = 50
    _MODE_CHANGE_LIMIT: int = 3
    _MODE_CHANGE_WINDOW_S: float = 3600.0
    _ENDPOINT_LIMIT: int = 60
    _ENDPOINT_WINDOW_S: float = 60.0

    def __init__(self) -> None:
        self.ready = False
        self.orchestrator: Orchestrator | None = None  # set in lifespan after startup()
        # SCAN3-013: bounded set + lock replaces plain list — prevents TOCTOU race
        # on concurrent WS connects that could exceed _MAX_WS_CLIENTS.
        self._ws_clients: set[WebSocket] = set()
        self._ws_lock: asyncio.Lock = asyncio.Lock()
        # SCAN3-015: deque with maxlen prevents unbounded growth under request flood.
        # Each deque entry is a monotonic timestamp (float). maxlen=_ENDPOINT_LIMIT
        # means we never store more than the limit allows, and popleft() is O(1).
        self._mode_change_ts: collections.deque[float] = collections.deque(
            maxlen=self._MODE_CHANGE_LIMIT
        )
        self._endpoint_hits: dict[str, collections.deque[float]] = {}

    @property
    def ws_clients(self) -> set[WebSocket]:
        """Read-only view of current WS clients (no locking — for iteration only)."""
        return self._ws_clients

    async def add_ws_client(self, ws: WebSocket) -> bool:
        """
        Atomically check-and-add a WS client.

        SCAN3-013: check-then-append is inside the lock so two concurrent
        connects can't both pass the capacity check before either appends.
        Returns True if added, False if at capacity.
        """
        async with self._ws_lock:
            if len(self._ws_clients) >= self._MAX_WS_CLIENTS:
                return False
            self._ws_clients.add(ws)
            return True

    async def remove_ws_client(self, ws: WebSocket) -> None:
        """Remove a WS client from the tracked set."""
        async with self._ws_lock:
            self._ws_clients.discard(ws)

    def check_endpoint_rate_limit(self, endpoint: str, client_ip: str = "") -> None:
        """
        Raise HTTP 429 if endpoint has been called too many times per minute.

        H-02: keyed by (endpoint, client_ip) so one IP cannot exhaust the
        rate budget for all other clients.
        M-12: prune stale IP entries to prevent unbounded dict growth under
        rotating-IP attacks.
        """
        key = f"{endpoint}:{client_ip}"
        now = time.monotonic()
        # M-12: prune expired entries before adding a new one
        stale = [
            k
            for k, dq in self._endpoint_hits.items()
            if not dq or (now - dq[-1]) > self._ENDPOINT_WINDOW_S * 2
        ]
        for k in stale:
            del self._endpoint_hits[k]
        dq = self._endpoint_hits.setdefault(key, collections.deque(maxlen=self._ENDPOINT_LIMIT))
        # Evict timestamps outside the window (O(1) per pop from left)
        while dq and now - dq[0] >= self._ENDPOINT_WINDOW_S:
            dq.popleft()
        if len(dq) >= self._ENDPOINT_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded for {endpoint} ({self._ENDPOINT_LIMIT}/min).",
            )
        dq.append(now)

    def check_mode_change_rate_limit(self) -> None:
        """
        Raise HTTP 429 if mode has been changed too many times recently.

        SCAN3-015: uses the same deque pattern as endpoint rate limiting.
        """
        now = time.monotonic()
        while self._mode_change_ts and now - self._mode_change_ts[0] >= self._MODE_CHANGE_WINDOW_S:
            self._mode_change_ts.popleft()
        if len(self._mode_change_ts) >= self._MODE_CHANGE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Execution mode change rate limit exceeded "
                    f"({self._MODE_CHANGE_LIMIT} per hour). Try again later."
                ),
            )
        self._mode_change_ts.append(now)


_state = AppState()

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize all subsystems on startup, clean up on shutdown."""
    # Fail fast on auth misconfiguration before accepting connections
    api_key = os.environ.get("API_SECRET_KEY", "").strip()
    if not api_key:
        raise RuntimeError("API_SECRET_KEY is not set. Set a strong random value in .env.")

    # H-01: Validate OPERATOR_SECRET at startup — prevents silent unavailability
    # of the /execution-mode endpoint in production.
    op_secret = os.environ.get("OPERATOR_SECRET", "").strip()
    if not op_secret:
        raise RuntimeError(
            "OPERATOR_SECRET is not set. Set a strong random value in .env. "
            "Generate with: openssl rand -hex 32"
        )

    cfg = get_settings()

    # SCAN3-012: validate_cors_config moved inside lifespan so misconfiguration
    # raises at startup time, not at import time (which caused confusing traces
    # in test suites and deployment tooling that imports the module for route inspection).
    validate_cors_config(cfg.api.cors_origins, allow_credentials=True)
    # Security: warn loudly if binding outside loopback without TLS proxy
    if cfg.api.host not in ("127.0.0.1", "::1", "localhost"):
        has_tls = bool(os.environ.get("HTTPS_CERT", "").strip())
        if not has_tls:
            log.critical(
                "api.insecure_bind_warning",
                host=cfg.api.host,
                message=(
                    "Server is bound to a non-loopback address without a TLS cert configured. "
                    "Set HTTPS_CERT or place a TLS-terminating reverse proxy in front. "
                    "API keys will be transmitted in cleartext."
                ),
            )
    # GAP-006: backend chosen by STORAGE_BACKEND (sqlite | timescale)
    _state.storage = create_storage_backend()
    await _state.storage.initialize()

    async with open_fetcher(_state.storage) as fetcher:
        _state.orchestrator = Orchestrator(_state.storage, fetcher)
        try:
            await _state.orchestrator.startup()
        except Exception as exc:
            # NEW-003: ensure fetcher connections are closed even when startup fails
            log.critical("api.startup_failed", error=str(exc))
            try:
                await fetcher.close()
            except Exception as close_exc:
                log.warning("api.fetcher_close_failed_on_startup_error", error=str(close_exc))
            raise
        _state.ready = True  # NEW-001: mark ready only after full startup

        orch_task = asyncio.create_task(_state.orchestrator.run(), name="orchestrator")

        # Self-tuning autostart: off by default (SelfTuningSettings.enabled
        # is the master kill switch — see src/config.py). When an operator
        # turns it on, this is the "explicit startup step" bootstrap.py's
        # module docstring requires; it never bypasses shadow_mode or the
        # promotion gate, both of which stay enforced inside TuningRunner.
        tuning_scheduler: AutoTuningScheduler | None = None
        if cfg.self_tuning.enabled:
            tuning_scheduler = AutoTuningScheduler(
                storage=_state.storage,
                settings=cfg,
                symbol=cfg.primary_symbol,
                timeframe=cfg.primary_timeframe.value,
            )
            tuning_scheduler.start()

        log.info("api.startup_complete", trading_mode=cfg.trading_mode.value)
        yield

        if tuning_scheduler is not None:
            tuning_scheduler.stop()
        _state.orchestrator.stop()
        try:
            await asyncio.wait_for(orch_task, timeout=10.0)
        except TimeoutError:
            orch_task.cancel()
        await _state.orchestrator.shutdown()
        await _state.storage.close()
        log.info("api.shutdown_complete")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Trade Bot API",
    version="1.0.0",
    lifespan=lifespan,
)

cfg = get_settings()

# SCAN3-012: CORS validation moved inside lifespan() — no longer runs at import time.
app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.api.cors_origins,
    allow_credentials=True,
    # M-06: restrict to actual HTTP methods used — GET and POST only
    allow_methods=["GET", "POST"],
    allow_headers=["x-api-key", "content-type"],
)

# ---------------------------------------------------------------------------
# Auth dependency — sole authentication mechanism for all endpoints
# ---------------------------------------------------------------------------


def api_key_header(x_api_key: str | None = Header(default=None, alias="x-api-key")) -> None:
    """FastAPI dependency — validates X-API-Key header on every request."""
    verify_api_key(x_api_key)


def require_ready() -> None:
    """Raises HTTP 503 if orchestrator is still starting (NEW-001)."""
    if not _state.ready:
        raise HTTPException(status_code=503, detail="Server starting up. Retry shortly.")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ResolveApprovalRequest(BaseModel):
    approved: bool
    operator: str = Field(..., min_length=1, max_length=64)
    # SEC-007: operator_secret required — same second-factor pattern as /execution-mode
    # so the approval audit trail can be trusted (operator field is server-verified, not
    # free text from any API-key holder).
    operator_secret: str = Field(
        ...,
        min_length=1,
        description="Must match OPERATOR_SECRET env var to authorise approval resolution",
    )

    @field_validator("operator")
    @classmethod
    def validate_operator(cls, v: str) -> str:
        return _validate_operator(v)


class SetExecutionModeRequest(BaseModel):
    mode: str
    operator: str = Field(..., min_length=1, max_length=64)
    operator_secret: str = Field(
        ...,
        min_length=1,
        description="Must match OPERATOR_SECRET env var to authorise mode escalation",
    )

    @field_validator("operator")
    @classmethod
    def validate_operator(cls, v: str) -> str:
        return _validate_operator(v)


class SetRiskControlsRequest(BaseModel):
    """
    GAP-013 -- runtime toggle/update for automated position-exit controls.

    All fields except operator/operator_secret are optional; only the
    fields actually supplied are changed (None = leave unchanged), letting
    the frontend toggle just stop_loss_enabled without resending every
    other value.
    """

    stop_loss_enabled: bool | None = None
    stop_loss_pct: float | None = Field(default=None, ge=0.1, le=50.0)
    take_profit_enabled: bool | None = None
    take_profit_pct: float | None = Field(default=None, ge=0.1, le=200.0)
    max_holding_period_s: float | None = Field(default=None, ge=60.0)
    operator: str = Field(..., min_length=1, max_length=64)
    operator_secret: str = Field(
        ...,
        min_length=1,
        description="Must match OPERATOR_SECRET env var to authorise a risk-control change",
    )

    @field_validator("operator")
    @classmethod
    def validate_operator(cls, v: str) -> str:
        return _validate_operator(v)


class SelfTuningPauseRequest(BaseModel):
    """Phase 6 -- runtime, no-restart pause switch for the self-tuning
    subsystem. Independent of the .env-level SELF_TUNING_ENABLED kill
    switch (that one requires a restart; this one is for an operator who
    needs to pause immediately without a deploy)."""

    operator: str = Field(..., min_length=1, max_length=64)
    operator_secret: str = Field(
        ...,
        min_length=1,
        description="Must match OPERATOR_SECRET env var to authorise pause/resume",
    )

    @field_validator("operator")
    @classmethod
    def validate_operator(cls, v: str) -> str:
        return _validate_operator(v)


class SelfTuningRollbackRequest(BaseModel):
    """Phase 6 -- manual forced revert of a tuned parameter to its
    previous version, bypassing the watchdog's own drift detection
    (e.g. an operator noticing a problem before automated drift
    detection would have)."""

    operator: str = Field(..., min_length=1, max_length=64)
    operator_secret: str = Field(
        ...,
        min_length=1,
        description="Must match OPERATOR_SECRET env var to authorise a manual rollback",
    )

    @field_validator("operator")
    @classmethod
    def validate_operator(cls, v: str) -> str:
        return _validate_operator(v)


def _verify_operator_secret(operator_secret: str, operator: str, endpoint: str) -> None:
    expected_op_secret = os.environ.get("OPERATOR_SECRET", "").strip()
    if not expected_op_secret:
        raise HTTPException(
            status_code=503,
            detail="OPERATOR_SECRET is not configured on the server.",
        )
    if not hmac.compare_digest(operator_secret.encode("utf-8"), expected_op_secret.encode("utf-8")):
        log.warning(f"api.{endpoint}_bad_operator_secret", operator=operator)
        raise HTTPException(status_code=401, detail="Invalid operator secret.")


# ---------------------------------------------------------------------------
# REST endpoints — all require API key
# ---------------------------------------------------------------------------


@app.get("/health", dependencies=[Depends(api_key_header)])
async def health() -> dict[str, Any]:
    """System health check — storage row counts, uptime."""
    counts = await _state.storage.health_check()
    return {
        "status": "ok",
        "storage": counts,
        "trading_mode": get_settings().trading_mode.value,
        "execution_mode": (await runtime_config.get_execution_mode()).value,
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }


@app.get("/metrics", dependencies=[Depends(api_key_header)])
async def prometheus_metrics() -> Any:
    """Prometheus text format metrics for Grafana scraping — TASK-007."""
    from fastapi.responses import Response

    body, content_type = metrics_output()
    return Response(content=body, media_type=content_type)


@app.get("/status", dependencies=[Depends(api_key_header), Depends(require_ready)])
async def status() -> dict[str, Any]:
    """Current equity, open positions, regime, execution mode."""
    from src.diagnostics.signal_debugger import get_degradation_tracker

    executor = cast(AbstractExecutor, _state.orchestrator._executor)
    cfg = get_settings()

    equity_usd = executor.equity_usd if executor else 0.0
    cash_usd = executor.cash_usd if executor else 0.0
    # H-10: use lock-safe variants to prevent RuntimeError from dict mutation
    # during concurrent close_position() in the event loop.
    positions = await executor.open_positions_safe() if executor else []
    approvals = await executor.pending_approvals_safe() if executor else []

    regime_snap = await _state.storage.latest_regime(
        cfg.primary_symbol, cfg.primary_timeframe.value
    )
    regime_dict: dict[str, Any] = {}
    if regime_snap is not None:
        regime_dict = {
            "state": regime_snap.regime_state,
            "prob_ranging": regime_snap.prob_ranging,
            "prob_trending": regime_snap.prob_trending,
            "prob_volatile": regime_snap.prob_volatile,
        }

    return {
        "equity_usd": round(equity_usd, 2),
        "cash_usd": round(cash_usd, 2),
        "starting_capital_usd": cfg.starting_capital_usd,
        "open_positions": positions,
        "pending_approvals": approvals,
        "regime": regime_dict,
        "predictions": get_degradation_tracker(cfg.primary_timeframe.value).prediction_stats(),
        "trading_mode": cfg.trading_mode.value,
        "execution_mode": (await runtime_config.get_execution_mode()).value,
        "primary_symbol": cfg.primary_symbol,
        "primary_timeframe": cfg.primary_timeframe.value,
        # H-08: truncate error strings — full tracebacks may leak internal paths/filenames
        "last_retrain_errors": {
            tf: str(err)[:200] for tf, err in _state.orchestrator._last_retrain_error.items()
        },
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }


@app.get("/trades", dependencies=[Depends(api_key_header)])
async def trades(
    symbol: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> dict[str, Any]:
    """Paginated trade history — offset and limit applied in SQL, not Python."""
    cfg = get_settings()
    req_symbol = symbol or cfg.primary_symbol
    # H-03: validate_symbol raises ValueError — convert to HTTP 400 so the
    # client sees a clear error instead of a 500 internal server error.
    try:
        await _state.storage.validate_symbol(req_symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    records = await _state.storage.fetch_trades(
        symbol=req_symbol,
        trading_mode=cfg.trading_mode.value,
        limit=limit,
        offset=offset,
    )
    return {
        "trades": [
            {
                "id": t.id,
                "symbol": t.symbol,
                "timeframe": t.timeframe,
                "direction": "long" if t.direction == 1 else "short",
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.quantity,
                "notional_usd": round(t.notional_usd, 2),
                "pnl_usd": round(t.pnl_usd, 4) if t.pnl_usd is not None else None,
                "pnl_pct": round(t.pnl_pct * 100, 3) if t.pnl_pct is not None else None,
                "fee_usd": round(t.fee_usd, 4),
                "kelly_fraction": round(t.kelly_fraction, 4),
                "regime_at_entry": t.regime_at_entry,
                "meta_label_prob": round(t.meta_label_prob, 4),
                "exit_reason": t.exit_reason,
                "approved_by": t.approved_by,
                "entry_ts": t.entry_ts,
                "exit_ts": t.exit_ts,
            }
            for t in records
        ],
        "total": len(records),
        "limit": limit,
        "offset": offset,
    }


@app.get("/missed-trades", dependencies=[Depends(api_key_header)])
async def missed_trades(
    symbol: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """UI-001: tradeable signals that never opened a position — gate
    rejection, approval denial/timeout, or a drift block."""
    cfg = get_settings()
    req_symbol = symbol or cfg.primary_symbol
    try:
        await _state.storage.validate_symbol(req_symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    records = await _state.storage.fetch_missed_trades(symbol=req_symbol, limit=limit)
    return {
        "missed_trades": [
            {
                "id": m.id,
                "symbol": m.symbol,
                "timeframe": m.timeframe,
                "direction": "long" if m.direction == 1 else "short",
                "reason": m.reason,
                "kelly_fraction": round(m.kelly_fraction, 4),
                "meta_label_prob": round(m.meta_label_prob, 4),
                "raw_signal": round(m.raw_signal, 4) if m.raw_signal is not None else None,
                "regime_at_entry": m.regime_at_entry,
                "notional_usd": round(m.notional_usd, 2),
                "ts": m.ts,
            }
            for m in records
        ],
        "total": len(records),
        "limit": limit,
    }


@app.get("/equity", dependencies=[Depends(api_key_header)])
async def equity_curve(
    limit: Annotated[int, Query(ge=1, le=10000)] = 1440,
) -> dict[str, Any]:
    """Equity curve data for charting."""
    cfg = get_settings()
    records = await _state.storage.fetch_equity_curve(
        trading_mode=cfg.trading_mode.value,
        limit=limit,
    )
    return {
        "curve": [
            {
                "ts": r.ts,
                "equity_usd": round(r.equity_usd, 2),
                "cash_usd": round(r.cash_usd, 2),
                "unrealized_pnl": round(r.unrealized_pnl, 4),
                "daily_pnl_usd": round(r.daily_pnl_usd, 4),
                "daily_pnl_pct": round(r.daily_pnl_pct, 4),
                "drawdown_pct": round(r.drawdown_pct, 4),
            }
            for r in records
        ]
    }


@app.get(
    "/regime/{timeframe}",
    dependencies=[Depends(api_key_header)],
    responses={
        404: {"description": "No regime data for timeframe"},
        400: {"description": "Invalid timeframe value"},
    },
)
async def regime(timeframe: str) -> dict[str, Any]:
    """Latest regime snapshot for a timeframe."""
    # SCAN3-004: Timeframe imported at module level — inline import removed
    valid_timeframes = {tf.value for tf in Timeframe}
    if timeframe not in valid_timeframes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid timeframe {timeframe!r}. Must be one of: {sorted(valid_timeframes)}",
        )
    cfg = get_settings()
    snap = await _state.storage.latest_regime(cfg.primary_symbol, timeframe)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"No regime data for timeframe={timeframe}")
    return {
        "symbol": snap.symbol,
        "timeframe": snap.timeframe,
        "ts": snap.ts,
        "regime_state": snap.regime_state,
        "regime_name": ["ranging", "trending", "volatile"][snap.regime_state],
        "prob_ranging": round(snap.prob_ranging, 4),
        "prob_trending": round(snap.prob_trending, 4),
        "prob_volatile": round(snap.prob_volatile, 4),
    }


@app.get("/approvals", dependencies=[Depends(api_key_header), Depends(require_ready)])
async def approvals() -> dict[str, Any]:
    """All pending approval requests."""
    executor = cast(AbstractExecutor, _state.orchestrator._executor)
    if executor is None:
        return {"approvals": []}
    return {"approvals": executor.pending_approvals()}


@app.post(
    "/approvals/{request_id}/resolve",
    dependencies=[Depends(api_key_header), Depends(require_ready)],
    responses={
        503: {"description": "Executor not initialized"},
        404: {"description": "Approval request not found or already resolved"},
    },
)
async def resolve_approval(
    request_id: str,
    body: ResolveApprovalRequest,
    request: Request,
) -> dict[str, Any]:
    """Approve or reject a pending trade."""
    # H-02: pass client IP so rate limit is per-IP, not global
    _state.check_endpoint_rate_limit(
        "resolve_approval", request.client.host if request.client else ""
    )
    # SEC-007: verify operator_secret (same second-factor pattern as /execution-mode)
    # The approval endpoint is the human-in-the-loop gate before a live trade executes;
    # without this check any holder of the API key can approve with any operator name,
    # undermining both the audit trail and the intent of the approval workflow.
    _expected_op_secret = os.environ.get("OPERATOR_SECRET", "").strip()
    if not _expected_op_secret:
        raise HTTPException(
            status_code=503,
            detail="OPERATOR_SECRET is not configured on the server.",
        )
    if not hmac.compare_digest(
        body.operator_secret.encode("utf-8"),
        _expected_op_secret.encode("utf-8"),
    ):
        log.warning("api.resolve_approval_bad_operator_secret", operator=body.operator)
        raise HTTPException(status_code=401, detail="Invalid operator secret.")
    # H-13: validate UUID format before dict lookup — prevents timing oracle and DoS
    if not _UUID_RE.match(request_id):
        raise HTTPException(status_code=400, detail="Invalid request_id format.")
    executor = cast(AbstractExecutor, _state.orchestrator._executor)
    if executor is None:
        raise HTTPException(status_code=503, detail="Executor not initialized")

    found = await executor.resolve_approval(
        request_id=request_id,
        approved=body.approved,
        operator=body.operator,
    )
    if not found:
        raise HTTPException(
            status_code=404,
            detail=f"Approval request {request_id!r} not found or already resolved",
        )
    return {"resolved": True, "approved": body.approved, "operator": body.operator}


@app.post(
    "/execution-mode",
    dependencies=[Depends(api_key_header)],
    responses={
        400: {"description": "Invalid execution mode"},
        401: {"description": "Invalid operator secret"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def set_execution_mode(body: SetExecutionModeRequest) -> dict[str, Any]:
    """
    Switch execution mode at runtime.

    Requires:
      - Valid X-API-Key header
      - operator_secret matching OPERATOR_SECRET env var
      - Max 3 mode changes per hour
    """
    # Verify operator secret (second factor for mode escalation)
    expected_op_secret = os.environ.get("OPERATOR_SECRET", "").strip()
    if not expected_op_secret:
        raise HTTPException(
            status_code=503,
            detail="OPERATOR_SECRET is not configured on the server.",
        )
    # SCAN3-003: hmac now at module level
    if not hmac.compare_digest(
        body.operator_secret.encode("utf-8"),
        expected_op_secret.encode("utf-8"),
    ):
        log.warning("api.execution_mode_bad_operator_secret", operator=body.operator)
        raise HTTPException(status_code=401, detail="Invalid operator secret.")

    # Rate limit: max 3 changes per hour
    _state.check_mode_change_rate_limit()

    try:
        new_mode = ExecutionMode(body.mode.lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode {body.mode!r}. Must be one of: automatic, restricted, manual",
        )

    old_mode = (await runtime_config.get_execution_mode()).value
    await runtime_config.set_execution_mode(new_mode)  # SCAN2-015: async — no event loop block

    await _state.storage.insert_audit_event(
        event_type="execution_mode_change",
        operator=body.operator,
        details={"old_mode": old_mode, "new_mode": new_mode.value},
    )

    log.info(
        "api.execution_mode_changed",
        new_mode=new_mode.value,
        old_mode=old_mode,
        operator=body.operator,
    )
    return {"execution_mode": new_mode.value, "operator": body.operator}


@app.get("/risk-controls", dependencies=[Depends(api_key_header), Depends(require_ready)])
async def get_risk_controls() -> dict[str, Any]:
    """
    GAP-013 -- read current automated position-exit control state
    (stop-loss / take-profit toggles + thresholds, max holding period).

    Read-only, no operator_secret required -- matches the rest of the
    read-only GET endpoints (api_key_header is sufficient).
    """
    controls = await runtime_config.get_risk_controls()
    return {"risk_controls": controls}


@app.post(
    "/risk-controls",
    dependencies=[Depends(api_key_header), Depends(require_ready)],
    responses={
        400: {"description": "Invalid risk-control value"},
        401: {"description": "Invalid operator secret"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def set_risk_controls(body: SetRiskControlsRequest, request: Request) -> dict[str, Any]:
    """
    Toggle/update automated position-exit controls at runtime.

    Requires:
      - Valid X-API-Key header
      - operator_secret matching OPERATOR_SECRET env var (same second
        factor as POST /execution-mode -- enabling/disabling a position's
        only automated exit path is just as consequential as switching
        execution mode, so it gets the same protection, closing the
        asymmetry flagged in SEC-007 for this specific control surface).
      - Standard per-endpoint rate limit (60/min, keyed by client IP)

    At least one of the optional fields must be supplied, or this is a
    no-op that still returns the current (unchanged) state.
    """
    _state.check_endpoint_rate_limit(
        "set_risk_controls", request.client.host if request.client else ""
    )

    expected_op_secret = os.environ.get("OPERATOR_SECRET", "").strip()
    if not expected_op_secret:
        raise HTTPException(
            status_code=503,
            detail="OPERATOR_SECRET is not configured on the server.",
        )
    if not hmac.compare_digest(
        body.operator_secret.encode("utf-8"),
        expected_op_secret.encode("utf-8"),
    ):
        log.warning("api.risk_controls_bad_operator_secret", operator=body.operator)
        raise HTTPException(status_code=401, detail="Invalid operator secret.")

    old_controls = await runtime_config.get_risk_controls()
    new_controls = await runtime_config.set_risk_controls(
        stop_loss_enabled=body.stop_loss_enabled,
        stop_loss_pct=body.stop_loss_pct,
        take_profit_enabled=body.take_profit_enabled,
        take_profit_pct=body.take_profit_pct,
        max_holding_period_s=body.max_holding_period_s,
    )

    await _state.storage.insert_audit_event(
        event_type="risk_controls_change",
        operator=body.operator,
        details={"old": old_controls, "new": new_controls},
    )

    log.info(
        "api.risk_controls_changed",
        old=old_controls,
        new=new_controls,
        operator=body.operator,
    )
    return {"risk_controls": new_controls, "operator": body.operator}


@app.get(
    "/model-metrics",
    dependencies=[Depends(api_key_header)],
    responses={400: {"description": "Invalid timeframe value"}},
)
async def model_metrics(timeframe: str | None = None) -> dict[str, Any]:
    """Latest OOS metrics for direction and meta-label models."""
    cfg = get_settings()
    tf = timeframe or cfg.primary_timeframe.value
    # UI-006: same validation as /regime/{timeframe} — an unchecked timeframe
    # string previously reached storage.latest_model_metrics/live_gate_passes
    # unvalidated, inconsistent with every other timeframe-taking endpoint.
    valid_timeframes = {t.value for t in Timeframe}
    if tf not in valid_timeframes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid timeframe {tf!r}. Must be one of: {sorted(valid_timeframes)}",
        )
    dir_m = await _state.storage.latest_model_metrics("direction", tf)
    meta_m = await _state.storage.latest_model_metrics("meta_label", tf)

    def _fmt(m: Any) -> dict[str, Any] | None:
        if m is None:
            return None
        return {
            "model_name": m.model_name,
            "timeframe": m.timeframe,
            "version": m.version,
            "oos_sharpe": round(m.oos_sharpe, 4),
            "max_drawdown": round(m.max_drawdown, 4),
            "n_trades": m.n_trades,
            "accuracy": round(m.accuracy, 4),
            "f1_score": round(m.f1_score, 4),
            "live_gate_pass": m.live_gate_pass,
        }

    return {
        "timeframe": tf,
        "direction": _fmt(dir_m),
        "meta_label": _fmt(meta_m),
        "live_gate_passes": await _state.storage.live_gate_passes(tf),
    }


# ---------------------------------------------------------------------------
# Self-tuning operator API (Phase 6) — see docs/SELF_TUNING_IMPLEMENTATION_PLAN.md
#
# Read-only status matches the api_key_header-only pattern used elsewhere
# for GET endpoints. Pause/resume/rollback follow the /risk-controls
# pattern: operator_secret required (same second factor as execution-mode
# changes), rate-limited, audited.
# ---------------------------------------------------------------------------


@app.get("/self-tuning/status", dependencies=[Depends(api_key_header)])
async def self_tuning_status() -> dict[str, Any]:
    """
    Registry state, current champion values, and probation status for
    every registered self-tuning parameter, plus recent audit history.
    """
    cfg = get_settings().self_tuning
    paused = await tuning_pause_state.is_paused()

    params: list[dict[str, Any]] = []
    for param in tuning_registry.list_all():
        current_version = None
        if tuning_version_store.has_versions(param.name):
            v = tuning_version_store.current(param.name)
            current_version = {
                "value": v.value,
                "version": v.version,
                "timestamp": v.timestamp,
                "promoted_by": v.promoted_by,
                "is_rollback": v.is_rollback,
            }
        recent_events = [
            {"event_type": e.event_type.value, "timestamp": e.timestamp, "details": e.details}
            for e in tuning_audit_log.read_for_param(param.name)[-10:]
        ]
        params.append(
            {
                "name": param.name,
                "description": param.description,
                "floor": param.floor,
                "ceiling": param.ceiling,
                "registered_current": param.current,
                "current_version": current_version,
                "probation_status": tuning_watchdog.probation_status(param.name).value,
                "recent_events": recent_events,
            }
        )

    return {
        "enabled": cfg.enabled,
        "shadow_mode": cfg.shadow_mode,
        "paused": paused,
        "parameters": params,
    }


@app.post(
    "/self-tuning/pause",
    dependencies=[Depends(api_key_header), Depends(require_ready)],
    responses={
        401: {"description": "Invalid operator secret"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def self_tuning_pause(body: SelfTuningPauseRequest, request: Request) -> dict[str, Any]:
    """Runtime pause, no restart required. Blocks new attempt() calls at
    the caller level (this flag is advisory to callers of TuningRunner.attempt,
    e.g. scripts/run_tuning_attempt.py and any future scheduler, not enforced
    inside TuningRunner itself -- see src/tuning/state.py)."""
    _state.check_endpoint_rate_limit(
        "self_tuning_pause", request.client.host if request.client else ""
    )
    _verify_operator_secret(body.operator_secret, body.operator, "self_tuning_pause")
    await tuning_pause_state.set_paused(True)
    tuning_audit_log.record("__global__", TuningEventType.PAUSED, {"operator": body.operator})
    log.info("api.self_tuning_paused", operator=body.operator)
    return {"paused": True, "operator": body.operator}


@app.post(
    "/self-tuning/resume",
    dependencies=[Depends(api_key_header), Depends(require_ready)],
    responses={
        401: {"description": "Invalid operator secret"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def self_tuning_resume(body: SelfTuningPauseRequest, request: Request) -> dict[str, Any]:
    _state.check_endpoint_rate_limit(
        "self_tuning_resume", request.client.host if request.client else ""
    )
    _verify_operator_secret(body.operator_secret, body.operator, "self_tuning_resume")
    await tuning_pause_state.set_paused(False)
    tuning_audit_log.record("__global__", TuningEventType.RESUMED, {"operator": body.operator})
    log.info("api.self_tuning_resumed", operator=body.operator)
    return {"paused": False, "operator": body.operator}


@app.post(
    "/self-tuning/rollback/{param_name}",
    dependencies=[Depends(api_key_header), Depends(require_ready)],
    responses={
        401: {"description": "Invalid operator secret"},
        404: {"description": "Parameter has no version history"},
        429: {"description": "Rate limit exceeded"},
    },
)
async def self_tuning_rollback(
    param_name: str, body: SelfTuningRollbackRequest, request: Request
) -> dict[str, Any]:
    """Manual forced revert to the previous promoted version."""
    _state.check_endpoint_rate_limit(
        "self_tuning_rollback", request.client.host if request.client else ""
    )
    _verify_operator_secret(body.operator_secret, body.operator, "self_tuning_rollback")

    try:
        reverted = tuning_version_store.rollback(param_name)
    except (NoVersionsError, NoPriorVersionError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    tuning_audit_log.record(
        param_name,
        TuningEventType.ROLLED_BACK,
        {"operator": body.operator, "reverted_to_value": reverted.value, "manual": True},
    )
    log.info(
        "api.self_tuning_manual_rollback",
        param=param_name,
        value=reverted.value,
        operator=body.operator,
    )
    return {"param_name": param_name, "reverted_value": reverted.value, "operator": body.operator}


# ---------------------------------------------------------------------------
# WebSocket — live push (auth required)
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """
    Live WebSocket feed — requires X-Api-Key header on upgrade.

    Pushes a status snapshot every ws_heartbeat_s seconds.
    Max concurrent clients: _MAX_WS_CLIENTS (default 50, set on AppState).
    """
    # C-02: Auth before any state mutation — prevents slot leak if auth raises
    # after add_ws_client succeeds but before accept().
    await verify_ws_key(ws)

    # Capacity check — only after auth passes
    added = await _state.add_ws_client(ws)
    if not added:
        await ws.close(code=4429)
        log.warning("api.ws_rejected_limit", limit=_state._MAX_WS_CLIENTS)
        return

    await ws.accept()
    cfg = get_settings()
    heartbeat = cfg.api.ws_heartbeat_s
    log.info("api.ws_connected", client=str(ws.client))

    try:
        while True:
            await asyncio.sleep(heartbeat)

            if _state.orchestrator is None:
                continue  # Server still starting — skip tick, retry next heartbeat
            executor = cast(AbstractExecutor, _state.orchestrator._executor)
            if executor is None:
                continue

            payload: dict[str, Any] = {
                "type": "tick",
                "equity_usd": round(executor.equity_usd, 2),
                "cash_usd": round(executor.cash_usd, 2),
                # Use lock-safe variants to prevent RuntimeError from dict mutation
                # during concurrent position open/close (VUL-035)
                "positions": await executor.open_positions_safe(),
                "pending_approvals": await executor.pending_approvals_safe(),
                "trading_mode": get_settings().trading_mode.value,
                "execution_mode": (await runtime_config.get_execution_mode()).value,
                "timestamp": datetime.now(tz=UTC).isoformat(),
            }

            snap = await _state.storage.latest_regime(
                cfg.primary_symbol, cfg.primary_timeframe.value
            )
            if snap is not None:
                payload["regime"] = {
                    "state": snap.regime_state,
                    "name": ["ranging", "trending", "volatile"][snap.regime_state],
                    "prob_ranging": round(snap.prob_ranging, 4),
                    "prob_trending": round(snap.prob_trending, 4),
                    "prob_volatile": round(snap.prob_volatile, 4),
                }

            await ws.send_text(json.dumps(payload))

    except WebSocketDisconnect:
        log.info("api.ws_disconnected", client=str(ws.client))
    except Exception as exc:
        log.error("api.ws_error", error=str(exc))
    finally:
        # SCAN3-013: thread-safe removal via locked method
        await _state.remove_ws_client(ws)


# ---------------------------------------------------------------------------
# Debug / diagnostics endpoints  (Patch C)
# ---------------------------------------------------------------------------


@app.get("/debug/health", dependencies=[Depends(api_key_header)])
async def debug_health() -> dict[str, Any]:
    """Runtime monitor snapshot — probes, alerts, memory, tick-stall status."""
    from src.diagnostics.runtime_monitor import get_monitor

    snap = get_monitor().get_snapshot()
    return (
        snap.to_dict() if snap else {"overall": "monitor_not_started", "probes": [], "alerts": []}
    )


@app.get("/debug/audit", dependencies=[Depends(api_key_header)])
async def debug_audit(
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """Trade decision audit — last N tick decisions with features, probabilities, gate chain, outcome."""
    from src.diagnostics.trade_auditor import get_auditor

    aud = get_auditor()
    return {
        "summary": aud.summary(),
        "anomalies": aud.anomaly_scan(),
        "recent": [r.to_dict() for r in aud.recent(limit)],
    }


@app.get("/debug/drift", dependencies=[Depends(api_key_header)])
async def debug_drift() -> dict[str, Any]:
    """Feature drift (KS test vs training baseline) + model degradation report."""
    from src.diagnostics.signal_debugger import get_degradation_tracker, get_drift_monitor

    drift_records = get_drift_monitor().check_all()
    return {
        "feature_drift": [
            {
                "feature": r.feature,
                "ks_statistic": r.ks_statistic,
                "drifted": r.drifted,
                "train_mean": r.train_mean,
                "live_mean": r.live_mean,
                "train_std": r.train_std,
                "live_std": r.live_std,
            }
            for r in drift_records
        ],
        "drifted_features": [r.feature for r in drift_records if r.drifted],
        "model_degradation": get_degradation_tracker(
            get_settings().primary_timeframe.value
        ).check_degradation(),
    }


@app.get("/debug/attribution", dependencies=[Depends(api_key_header)])
async def debug_attribution(
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
    trading_mode: str = "paper",
) -> dict[str, Any]:
    """P&L attribution sliced by regime, timeframe, direction, and model-confidence quartile."""
    from src.diagnostics.attribution import build_attribution

    trades = await _state.storage.fetch_trades(trading_mode=trading_mode, limit=limit)
    report = build_attribution(trades)
    return report.to_dict()


@app.get("/journal/summary", tags=["diagnostics"], dependencies=[Depends(api_key_header)])
async def journal_summary(
    limit: Annotated[int, Query(ge=1, le=5000)] = 1000,
    trading_mode: str = "paper",
) -> dict[str, Any]:
    """
    Structured trade journal summary with P&L decomposition.

    Returns aggregate stats (win rate, fee drag, slippage cost) broken down
    by regime and exit reason across the most recent closed trades.
    """
    from src.diagnostics.trade_journal import build_journal, summarise_journal

    trades = await _state.storage.fetch_trades(trading_mode=trading_mode, limit=limit)
    entries = build_journal(trades)
    summary = summarise_journal(entries)
    return summary.to_dict()


@app.get("/debug/kill-switch", dependencies=[Depends(api_key_header)])
async def debug_kill_switch(
    symbol: str = "BTC/USDT:USDT",
    timeframe: str = "1h",
) -> dict[str, Any]:
    """Per-strategy circuit breaker status (consecutive losses, win-rate, drawdown gates)."""
    from src.risk.strategy_kill_switch import get_kill_switch

    ks = get_kill_switch()
    key = f"{symbol}:{timeframe}"
    state = ks._states.get(key)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "is_active": ks.is_active(symbol, timeframe),
        "state": state.__dict__ if state is not None else None,
    }


@app.get("/debug/capital-floor", dependencies=[Depends(api_key_header)])
async def debug_capital_floor() -> dict[str, Any]:
    """Capital preservation floor status (HWM ratchet + absolute max-loss gate)."""
    from src.risk.capital_preservation_floor import CapitalPreservationFloor

    # Return schema only — actual instance lives in orchestrator / engine
    return {
        "info": "Instantiate CapitalPreservationFloor per-account in the engine.",
        "default_params": {
            "trigger_pct": 0.10,
            "lock_in_pct": 0.05,
            "max_loss_pct": 0.20,
        },
        "class": CapitalPreservationFloor.__module__ + ".CapitalPreservationFloor",
    }


@app.get("/debug/macro-budget", dependencies=[Depends(api_key_header)])
async def debug_macro_budget() -> dict[str, Any]:
    """Cross-asset macro exposure budget utilisation across all asset groups."""
    from src.risk.macro_exposure_budget import _REGISTRY

    if _REGISTRY is None:
        return {"status": "not_initialised", "summary": None}
    return {"status": "ok", "summary": _REGISTRY.summary()}


@app.get("/debug/regime-pulse", dependencies=[Depends(api_key_header)])
async def debug_regime_pulse(
    symbol: str = "BTC/USDT:USDT",
    timeframe: str = "1h",
) -> dict[str, Any]:
    """
    Single-glance trading health check.

    Aggregates regime state, strategy selection, kill-switch status, and
    macro-budget utilisation so operators can confirm the system is ready
    to trade without querying multiple endpoints.

    Returns
    -------
    {
        "ready_to_trade": bool,
        "regime": {...},
        "strategy_selected": str,
        "kill_switch_active": bool,
        "macro_budget_ok": bool,
        "gates": {regime, kill_switch, macro_budget}
    }
    """
    from src.risk.macro_exposure_budget import _REGISTRY
    from src.risk.strategy_kill_switch import get_kill_switch
    from src.strategies.regime_strategy_selector import (
        STRATEGY_NEUTRAL,
        select_strategy,
    )

    # Regime from storage
    regime_row = await _state.storage.latest_regime(timeframe)
    if regime_row is not None:
        regime_state = int(regime_row.get("state", 1))
        confidence = float(regime_row.get("confidence", 0.5))
        entropy = float(regime_row.get("entropy", 0.5))
        is_transition = bool(regime_row.get("is_transition", False))
    else:
        regime_state, confidence, entropy, is_transition = 1, 0.5, 0.5, False

    # Strategy selection
    selection = select_strategy(
        regime_state=regime_state,
        confidence=confidence,
        entropy=entropy,
        is_transition=is_transition,
    )

    # Kill switch
    ks = get_kill_switch()
    kill_switch_active = not ks.is_active(symbol, timeframe)

    # Macro budget
    macro_ok = True
    if _REGISTRY is not None:
        summary = _REGISTRY.summary()
        macro_ok = summary.get("global_utilisation_pct", 0.0) < 95.0

    ready = selection.strategy != STRATEGY_NEUTRAL and not kill_switch_active and macro_ok

    return {
        "ready_to_trade": ready,
        "symbol": symbol,
        "timeframe": timeframe,
        "regime": {
            "state": regime_state,
            "confidence": round(confidence, 4),
            "entropy": round(entropy, 4),
            "is_transition": is_transition,
        },
        "strategy_selected": selection.strategy,
        "strategy_reject_reason": selection.reject_reason,
        "kill_switch_active": kill_switch_active,
        "macro_budget_ok": macro_ok,
        "gates": {
            "regime_ok": selection.strategy != STRATEGY_NEUTRAL,
            "kill_switch_ok": not kill_switch_active,
            "macro_budget_ok": macro_ok,
        },
    }


@app.get("/settings/strategy", tags=["settings"], dependencies=[Depends(api_key_header)])
async def get_strategy_settings() -> dict[str, Any]:
    """
    Current StrategySettings values (mean-reversion, breakout, regime-selector).

    Reports the active thresholds from STRATEGY_* env vars so operators can
    verify what parameters are in effect without reading .env files directly.
    """
    cfg = get_settings().strategy
    return {
        "mean_reversion": {
            "mr_lookback": cfg.mr_lookback,
            "mr_entry_z": cfg.mr_entry_z,
            "mr_exit_z": cfg.mr_exit_z,
            "mr_min_half_life": cfg.mr_min_half_life,
            "mr_max_half_life": cfg.mr_max_half_life,
            "mr_require_ou": cfg.mr_require_ou,
        },
        "breakout": {
            "bo_entry_period": cfg.bo_entry_period,
            "bo_exit_period": cfg.bo_exit_period,
            "bo_atr_period": cfg.bo_atr_period,
            "bo_min_atr_pct": cfg.bo_min_atr_pct,
            "bo_max_atr_pct": cfg.bo_max_atr_pct,
        },
        "regime_selector": {
            "rs_min_confidence": cfg.rs_min_confidence,
            "rs_max_entropy": cfg.rs_max_entropy,
            "rs_transition_guard": cfg.rs_transition_guard,
        },
    }


@app.get("/debug/order-throttler", dependencies=[Depends(api_key_header)])
async def debug_order_throttler() -> dict[str, Any]:
    """Per-exchange token-bucket rate-limiter status (tokens remaining, rate, burst)."""
    from src.execution.order_throttler import OrderThrottler

    # The throttler is stateless at module level; surface default params and
    # note that per-exchange state lives inside the orchestrator's instance.
    return {
        "info": "Per-exchange throttler state lives in the orchestrator instance.",
        "default_params": {
            "rate_per_second": 10.0,
            "burst": 20,
        },
        "class": OrderThrottler.__module__ + ".OrderThrottler",
        "status_method": "throttler.status()",
    }


@app.get("/debug/portfolio-correlation", dependencies=[Depends(api_key_header)])
async def debug_portfolio_correlation() -> dict[str, Any]:
    """EWM rolling pairwise correlation matrix for all tracked symbols."""
    from src.risk.portfolio_correlation import get_portfolio_correlation

    tracker = get_portfolio_correlation()
    symbols = tracker.tracked_symbols
    matrix = {f"{a}:{b}": v for (a, b), v in tracker.correlation_matrix().items()}
    return {
        "tracked_symbols": symbols,
        "n_symbols": len(symbols),
        "correlation_matrix": matrix,
    }


@app.post("/debug/selftest", dependencies=[Depends(api_key_header)])
async def debug_selftest() -> dict[str, Any]:
    """On-demand pipeline self-test — synthetic round-trip through feature pipeline."""
    from src.diagnostics.signal_debugger import run_pipeline_selftest

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, run_pipeline_selftest)
    return result


@app.get("/orders/{order_id}/status", tags=["execution"], dependencies=[Depends(api_key_header)])
async def get_order_status(
    order_id: str,
) -> dict[str, Any]:
    """
    Get order FSM state for reconciliation.

    Useful for debugging unconfirmed orders or recovering from network
    failures. Returns the order's state machine snapshot including:
      - Current status (PENDING, FILLING, FILLED, etc.)
      - Filled quantity and average fill price
      - Retry count and last error
      - Exchange response snapshot

    Parameters
    ----------
    order_id : Exchange order ID

    Returns
    -------
    Order FSM state snapshot (serialized)
    """
    # BUGFIX (found during audit, 2026-06-25): this previously read
    # runtime_config.executor, which does not exist on RuntimeConfig --
    # every other endpoint in this file accesses the live executor via
    # _state.orchestrator._executor (see /status, /model-metrics,
    # /debug/health for the same pattern). Also: LiveExecutor previously
    # discarded its OrderFSM state as a local variable once
    # _place_market_order() returned, so get_order_fsm_state() didn't exist
    # either -- added a bounded in-memory registry (see
    # _ORDER_FSM_REGISTRY_MAX_SIZE in live.py) so this endpoint can actually
    # serve real reconciliation data instead of always falling through to
    # the "not available" branch below.
    if _state.orchestrator is None:
        return {"error": "Orchestrator not initialised."}
    executor = cast(AbstractExecutor, _state.orchestrator._executor)
    if not hasattr(executor, "get_order_fsm_state"):
        # PaperExecutor never places real exchange orders, so it has no
        # order FSM to report -- this is the expected, correct response
        # in paper trading mode, not an error.
        return {"error": "Order FSM reconciliation is only available in live trading mode."}
    try:
        state = await executor.get_order_fsm_state(order_id)
    except Exception as exc:
        # UI-006: log the real exception server-side; never echo raw
        # exception text back to the (authenticated but untrusted) caller —
        # it can leak internal state/stack details for no operational benefit.
        log.warning("api.order_status_lookup_failed", order_id=order_id, error=str(exc))
        return {"error": "Failed to look up order status."}
    if state is None:
        return {
            "error": (
                "Order not found in the recent-order registry -- it may "
                "predate this process's startup, have aged out of the "
                "bounded recent-order registry, or never have been "
                "placed by this server."
            )
        }
    return state.to_dict()


@app.get("/performance-drift", tags=["monitoring"], dependencies=[Depends(api_key_header)])
async def get_performance_drift() -> dict[str, Any]:
    """
    Get current performance drift status.

    Monitors model degradation in live trading. Compares live performance
    metrics (Sharpe, accuracy, win rate, max DD) against training baseline.

    Returns
    -------
    {
        "drifted": bool,           # True if drift threshold exceeded
        "metric": str,             # Which metric drifted (if any)
        "reason": str,             # Explanation
        "drift_pp": float,         # Percentage points of drift
        "live_value": float,       # Current live metric value
        "baseline_value": float,   # Training baseline
        "metrics": {
            "total_live_trades": int,
            "rolling_sharpe": float,
            "rolling_winrate": float,
            "rolling_accuracy": float,
            "max_live_drawdown_pct": float,
        }
    }
    """
    # BUGFIX (found during audit, 2026-06-25): this previously read
    # runtime_config.drift_adapter, which does not exist on RuntimeConfig.
    # The orchestrator owns _drift_adapter as its own instance attribute
    # (initialised in Orchestrator.startup() once a trained model baseline
    # is available) -- access it the same way every other endpoint reaches
    # orchestrator-owned state (_state.orchestrator._executor etc.).
    if _state.orchestrator is None:
        return {"drifted": False, "reason": "Orchestrator not initialised."}
    try:
        return _state.orchestrator._drift_adapter.check_drift()
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/intelligence/coverage", tags=["intelligence"], dependencies=[Depends(api_key_header)])
async def get_intelligence_coverage() -> dict:
    """
    Return per-column non-NULL coverage for intelligence_features_history.

    Useful for operators to verify OCI backfill health and understand which
    features are active in the current training run.

    Returns
    -------
    {
        "symbol": str,
        "timeframe": str,
        "total_rows": int,
        "coverage": {"intelligence_<col>": float, ...}   # 0.0-1.0
    }
    """
    if _state.orchestrator is None:
        return {"error": "Orchestrator not initialised."}
    try:
        storage = _state.orchestrator._storage
        # BUG FIX: _state.runtime_config is not an AppState attribute — runtime_config
        # is a module-level singleton imported at line 51. Use get_settings() instead,
        # which is already used for symbol/timeframe throughout the rest of this file.
        cfg = get_settings()
        symbol = cfg.primary_symbol
        timeframe = (
            cfg.primary_timeframe.value
            if hasattr(cfg.primary_timeframe, "value")
            else str(cfg.primary_timeframe)
        )
        cov = await storage.intelligence_feature_coverage(symbol, timeframe)
        return {"symbol": symbol, "timeframe": timeframe, **cov}
    except Exception as exc:
        return {"error": str(exc)}


@app.get("/intelligence/providers", tags=["intelligence"], dependencies=[Depends(api_key_header)])
async def get_intelligence_providers() -> dict:
    """
    Return the current OCI provider configuration (which keys are active).

    Does NOT expose key values — only reports which providers are enabled
    (key non-empty) vs disabled (key empty → fail-open neutral mode).

    Returns
    -------
    {
        "providers": [
            {"name": str, "enabled": bool, "exchange_id": str}
        ]
    }
    """
    try:
        from src.config import get_settings

        cfg = get_settings().intelligence
        providers = [
            {
                "name": "ArkhamProvider",
                "exchange_id": "arkham_intel",
                "enabled": bool(cfg.arkham_api_key),
            },
            {
                "name": "DeFiLlamaProvider",
                "exchange_id": "defillama",
                "enabled": True,
            },  # public API
            {
                "name": "DuneProvider",
                "exchange_id": "dune_analytics",
                "enabled": bool(cfg.dune_api_key),
            },
            {
                "name": "CryptoQuantProvider",
                "exchange_id": "cryptoquant",
                "enabled": bool(cfg.cryptoquant_api_key),
            },
            {
                "name": "CoinglassProvider",
                "exchange_id": "coinglass",
                "enabled": bool(cfg.coinglass_api_key),
            },
        ]
        return {"providers": providers}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Risk simulation endpoint
# ---------------------------------------------------------------------------


class SizeCheckRequest(BaseModel):
    """Request body for /risk/size-check."""

    symbol: str = Field(..., description="Trading pair, e.g. BTC/USDT")
    group: str = Field(..., description="Asset group for macro-budget, e.g. crypto_large_cap")
    capital_usd: float = Field(..., gt=0, description="Total account capital in USD")
    current_equity: float = Field(..., gt=0, description="Current account equity in USD")
    hwm: float = Field(..., gt=0, description="High-water mark equity in USD")
    realized_vol_pct: float = Field(
        ..., ge=0, description="Annualised realised volatility as pct (e.g. 80.0 = 80%)"
    )
    win_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    avg_win_usd: float = Field(default=0.0, ge=0.0)
    avg_loss_usd: float = Field(default=0.0, ge=0.0)
    target_vol_pct: float = Field(
        default=1.0, gt=0, description="Daily vol target pct per position"
    )
    max_notional_pct: float = Field(default=0.25, gt=0, le=1.0)


@app.post("/risk/size-check", tags=["risk"], dependencies=[Depends(api_key_header)])
async def risk_size_check(body: SizeCheckRequest) -> dict[str, Any]:
    """
    Pre-trade feasibility check combining vol-target sizing and macro-budget.

    Simulates what position size would result from the vol-target sizer and
    whether the macro-exposure budget permits adding that notional to the
    given asset group.  Does not place any order.

    Returns
    -------
    {
        "vol_target": {notional_usd, vol_target_notional, kelly_scalar, ...},
        "budget_check": {allowed, group, requested_notional, ...},
        "final_notional_usd": float,
        "allowed": bool,
        "reject_reason": str
    }
    """
    from src.risk.macro_exposure_budget import get_budget
    from src.risk.vol_target_sizer import vol_target_size

    vt = vol_target_size(
        capital_usd=body.capital_usd,
        current_equity=body.current_equity,
        hwm=body.hwm,
        realized_vol_pct=body.realized_vol_pct,
        target_vol_pct=body.target_vol_pct,
        max_notional_pct=body.max_notional_pct,
        win_rate=body.win_rate,
        avg_win_usd=body.avg_win_usd,
        avg_loss_usd=body.avg_loss_usd,
    )

    budget = get_budget(capital_usd=body.capital_usd)
    bc = budget.check(
        symbol=body.symbol,
        group=body.group,
        requested_notional=vt.notional_usd,
    )

    allowed = bool(vt.notional_usd > 0 and bc.allowed)
    reject_reason = vt.reject_reason or ("" if bc.allowed else bc.reason)

    return {
        "vol_target": {
            "notional_usd": round(vt.notional_usd, 2),
            "vol_target_notional": round(vt.vol_target_notional, 2),
            "kelly_scalar": round(vt.kelly_scalar, 4),
            "dd_haircut": round(vt.dd_haircut, 4),
            "realized_vol_pct": round(vt.realized_vol_pct, 4),
            "reject_reason": vt.reject_reason,
        },
        "budget_check": {
            "allowed": bc.allowed,
            "group": bc.group,
            "requested_notional": round(bc.requested_notional, 2),
            "current_group_notional": round(bc.current_group_notional, 2),
            "current_global_notional": round(bc.current_global_notional, 2),
            "reason": bc.reason,
        },
        "final_notional_usd": round(vt.notional_usd, 2) if allowed else 0.0,
        "allowed": allowed,
        "reject_reason": reject_reason,
    }
