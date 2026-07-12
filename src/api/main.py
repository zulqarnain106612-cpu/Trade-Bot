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
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from src.api.auth import verify_api_key, verify_ws_key
from src.api.middleware import validate_cors_config
from src.config import (
    ExecutionMode,
    Timeframe,
    get_settings,
    runtime_config,
)
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
        raise ValueError("operator must be 1–64 alphanumeric/underscore/hyphen characters")
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

    def check_endpoint_rate_limit(self, endpoint: str) -> None:
        """
        Raise HTTP 429 if endpoint has been called too many times per minute.

        SCAN3-015: uses deque(maxlen=limit) per endpoint. Old timestamps are
        popped from the left in O(1). The maxlen bound caps memory regardless
        of request rate.
        """
        now = time.monotonic()
        dq = self._endpoint_hits.setdefault(
            endpoint, collections.deque(maxlen=self._ENDPOINT_LIMIT)
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
        raise RuntimeError("API_SECRET_KEY is not set. Set a strong random value in .env.")

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
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Auth dependency — sole authentication mechanism for all endpoints
# ---------------------------------------------------------------------------

from fastapi import Header  # noqa: E402 — placed after app init intentionally


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


@app.get("/status", dependencies=[Depends(api_key_header), Depends(require_ready)])
async def status() -> dict[str, Any]:
    """Current equity, open positions, regime, execution mode."""
    executor = cast(AbstractExecutor, _state.orchestrator._executor)
    cfg = get_settings()

    equity_usd = executor.equity_usd if executor else 0.0
    cash_usd = executor.cash_usd if executor else 0.0
    positions = executor.open_positions() if executor else []
    approvals = executor.pending_approvals() if executor else []

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
        # SCAN2-003: surface last retrain errors so operators know when models are stale
        "last_retrain_errors": dict(_state.orchestrator._last_retrain_error),
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }


@app.get("/trades", dependencies=[Depends(api_key_header)])
async def trades(
    symbol: Annotated[str | None, Query(default=None)] = None,
    limit: Annotated[int, Query(default=100, ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(default=0, ge=0, le=10000)] = 0,
) -> dict[str, Any]:
    """Paginated trade history — offset and limit applied in SQL, not Python."""
    cfg = get_settings()
    req_symbol = symbol or cfg.primary_symbol
    await _state.storage.validate_symbol(req_symbol)

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
    limit: Annotated[int, Query(default=1440, ge=1, le=10000)],
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
) -> dict[str, Any]:
    """Approve or reject a pending trade."""
    _state.check_endpoint_rate_limit("resolve_approval")
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
    # SCAN3-013: atomic check-and-add via locked method — no TOCTOU race
    await verify_ws_key(ws)
    if not await _state.add_ws_client(ws):
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
