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
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Annotated, AsyncIterator, cast

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from src.config import ExecutionMode, get_settings, invalidate_settings_cache
from src.data.fetcher import open_fetcher
from src.data.storage import StorageBackend
from src.execution.live import LiveExecutor
from src.engine.orchestrator import Orchestrator
from src.api.auth import verify_api_key, verify_ws_key
from src.api.middleware import validate_cors_config

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
    ws_clients: list[WebSocket]

    def __init__(self) -> None:
        self.ws_clients = []


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

    cfg = get_settings()
    _state.storage = StorageBackend()
    await _state.storage.initialize()

    async with open_fetcher(_state.storage) as fetcher:
        _state.orchestrator = Orchestrator(_state.storage, fetcher)
        await _state.orchestrator.startup()

        orch_task = asyncio.create_task(_state.orchestrator.run(), name="orchestrator")

        log.info("api.startup_complete", trading_mode=cfg.trading_mode.value)
        yield

        _state.orchestrator.stop()
        try:
            await asyncio.wait_for(orch_task, timeout=10.0)
        except asyncio.TimeoutError:
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

# Validate CORS config before adding middleware — raises on wildcard+credentials
validate_cors_config(cfg.api.cors_origins, allow_credentials=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cfg.api.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def require_api_key(x_api_key: str | None = Depends(
    lambda x_api_key: x_api_key  # populated from header by FastAPI
)) -> None:
    """FastAPI dependency — validates X-API-Key header on every request."""
    pass  # delegated below via Header()


from fastapi import Header  # noqa: E402 — placed after app init intentionally


def api_key_header(x_api_key: str | None = Header(default=None, alias="x-api-key")) -> None:
    verify_api_key(x_api_key)


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
        "execution_mode": get_settings().execution_mode.value,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }


@app.get("/status", dependencies=[Depends(api_key_header)])
async def status() -> dict[str, Any]:
    """Current equity, open positions, regime, execution mode."""
    orch = _state.orchestrator
    executor = cast(LiveExecutor, orch._executor)  # noqa: SLF001
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
        "execution_mode": cfg.execution_mode.value,
        "primary_symbol": cfg.primary_symbol,
        "primary_timeframe": cfg.primary_timeframe.value,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }


@app.get("/trades", dependencies=[Depends(api_key_header)])
async def trades(
    symbol: Annotated[str | None, Query(default=None)] = None,
    limit: Annotated[int, Query(default=100, ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(default=0, ge=0, le=100000)] = 0,
) -> dict[str, Any]:
    """Paginated trade history."""
    cfg = get_settings()
    # Validate symbol against known pair if provided
    req_symbol = symbol or cfg.primary_symbol
    await _state.storage.validate_symbol(req_symbol)

    records = await _state.storage.fetch_trades(
        symbol=req_symbol,
        trading_mode=cfg.trading_mode.value,
        limit=limit + offset,
    )
    page = records[offset: offset + limit]
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
            for t in page
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
    responses={404: {"description": "No regime data for timeframe"}},
)
async def regime(timeframe: str) -> dict[str, Any]:
    """Latest regime snapshot for a timeframe."""
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


@app.get("/approvals", dependencies=[Depends(api_key_header)])
async def approvals() -> dict[str, Any]:
    """All pending approval requests."""
    executor = cast(LiveExecutor, _state.orchestrator._executor)  # noqa: SLF001
    if executor is None:
        return {"approvals": []}
    return {"approvals": executor.pending_approvals()}


@app.post(
    "/approvals/{request_id}/resolve",
    dependencies=[Depends(api_key_header)],
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
    executor = cast(LiveExecutor, _state.orchestrator._executor)  # noqa: SLF001
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
    responses={400: {"description": "Invalid execution mode"}},
)
async def set_execution_mode(body: SetExecutionModeRequest) -> dict[str, Any]:
    """
    Switch execution mode at runtime.

    Requires authenticated operator identity (validated operator field).
    Change is written to audit log and settings cache is invalidated.
    """
    try:
        new_mode = ExecutionMode(body.mode.lower())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode {body.mode!r}. Must be one of: automatic, restricted, manual",
        )

    old_mode = get_settings().execution_mode.value
    os.environ["EXECUTION_MODE"] = new_mode.value
    invalidate_settings_cache()

    # Persist to audit log
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
    """
    await verify_ws_key(ws)
    await ws.accept()
    _state.ws_clients.append(ws)
    cfg = get_settings()
    heartbeat = cfg.api.ws_heartbeat_s

    log.info("api.ws_connected", client=str(ws.client))

    try:
        while True:
            await asyncio.sleep(heartbeat)

            executor = cast(LiveExecutor, _state.orchestrator._executor)  # noqa: SLF001
            if executor is None:
                continue

            payload: dict[str, Any] = {
                "type": "tick",
                "equity_usd": round(executor.equity_usd, 2),
                "cash_usd": round(executor.cash_usd, 2),
                "positions": executor.open_positions(),
                "pending_approvals": executor.pending_approvals(),
                "trading_mode": get_settings().trading_mode.value,
                "execution_mode": get_settings().execution_mode.value,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
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
        if ws in _state.ws_clients:
            _state.ws_clients.remove(ws)
