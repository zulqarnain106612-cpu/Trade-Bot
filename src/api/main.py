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


# H-13: UUID format regex — prevents timing oracle via huge string hash and DoS
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I
)
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from src.api.auth import verify_api_key, verify_ws_key
from src.api.metrics import metrics_output
from src.api.middleware import validate_cors_config
from src.config import ExecutionMode, Timeframe, get_settings, runtime_config
from src.data.fetcher import open_fetcher
from src.data.storage import StorageBackend
from src.engine.orchestrator import Orchestrator
from src.execution.base import AbstractExecutor


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Operator field validator — reused across request models
# ---------------------------------------------------------------------------

_OPERATOR_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def _validate_operator(v: str) -> str:
    if not _OPERATOR_RE.match(v):
        raise ValueError(
            "operator must be 1–64 alphanumeric/underscore/hyphen characters"
        )
    return v


# ---------------------------------------------------------------------------
# Application state — shared across requests
# ---------------------------------------------------------------------------


class AppState:
    storage: StorageBackend
    orchestrator: Orchestrator
    ready: bool  # True only after orchestrator.startup() completes
    _MAX_WS_CLIENTS: int = 50
    _MODE_CHANGE_LIMIT: int = 3
    _MODE_CHANGE_WINDOW_S: float = 3600.0
    _ENDPOINT_LIMIT: int = 60
    _ENDPOINT_WINDOW_S: float = 60.0

    def __init__(self) -> None:
        self.ready = False
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
            k for k, dq in self._endpoint_hits.items()
            if not dq or (now - dq[-1]) > self._ENDPOINT_WINDOW_S * 2
        ]
        for k in stale:
            del self._endpoint_hits[k]
        dq = self._endpoint_hits.setdefault(
            key, collections.deque(maxlen=self._ENDPOINT_LIMIT)
        )
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
        raise RuntimeError(
            "API_SECRET_KEY is not set. Set a strong random value in .env."
        )

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
    _state.storage = StorageBackend()
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

        log.info("api.startup_complete", trading_mode=cfg.trading_mode.value)
        yield

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

from fastapi import Header


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
        "trading_mode": cfg.trading_mode.value,
        "execution_mode": (await runtime_config.get_execution_mode()).value,
        "primary_symbol": cfg.primary_symbol,
        "primary_timeframe": cfg.primary_timeframe.value,
        # H-08: truncate error strings — full tracebacks may leak internal paths/filenames
        "last_retrain_errors": {
            tf: str(err)[:200]
            for tf, err in _state.orchestrator._last_retrain_error.items()
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
    _state.check_endpoint_rate_limit("resolve_approval", request.client.host if request.client else "")
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


@app.get("/model-metrics", dependencies=[Depends(api_key_header)])
async def model_metrics(timeframe: str | None = None) -> dict[str, Any]:
    """Latest OOS metrics for direction and meta-label models."""
    cfg = get_settings()
    tf = timeframe or cfg.primary_timeframe.value
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
    return snap.to_dict() if snap else {"overall": "monitor_not_started", "probes": [], "alerts": []}


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
        "model_degradation": get_degradation_tracker().check_degradation(),
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
        return {"error": str(exc)}
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
