"""
FastAPI backend — REST endpoints + WebSocket hub for dashboard.
"""
from __future__ import annotations
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.config       import get_settings
from src.data.storage import Database
from src.engine.orchestrator import Orchestrator

log = structlog.get_logger()

# ── Globals ──────────────────────────────────────────────────────────────────
_db:           Database     | None = None
_orchestrator: Orchestrator | None = None
_ws_clients:   list[WebSocket]     = []

async def broadcast(msg: dict[str, Any]) -> None:
    dead: list[WebSocket] = []
    for ws in _ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _orchestrator
    cfg = get_settings()

    os.makedirs("./data",   exist_ok=True)
    os.makedirs("./models", exist_ok=True)

    _db = Database(cfg.db_path)
    await _db.connect()

    _orchestrator = Orchestrator(db=_db, broadcast=broadcast)
    asyncio.create_task(_orchestrator.start(cfg))
    log.info("orchestrator starting", mode=cfg.trading_mode)

    yield

    log.info("shutting down")
    if _db:
        await _db.close()

app = FastAPI(title="Trade-Bot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── WebSocket ─────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    # Send current status immediately
    if _orchestrator:
        await ws.send_json({"type": "status", "data": _orchestrator.status()})
    try:
        while True:
            data = await ws.receive_json()
            await _handle_ws_message(ws, data)
    except WebSocketDisconnect:
        _ws_clients.remove(ws)

async def _handle_ws_message(ws: WebSocket, msg: dict[str, Any]) -> None:
    action = msg.get("action")
    if action == "approve":
        await _orchestrator.resolve_approval(str(msg["signal_id"]), True)
    elif action == "reject":
        await _orchestrator.resolve_approval(str(msg["signal_id"]), False)
    elif action == "ping":
        await ws.send_json({"type": "pong"})

# ── REST endpoints ────────────────────────────────────────────────────────────
@app.get("/api/status")
async def get_status() -> dict[str, Any]:
    return _orchestrator.status() if _orchestrator else {}

class ConfigUpdate(BaseModel):
    key:   str
    value: Any

@app.post("/api/config")
async def update_config(update: ConfigUpdate):
    await _orchestrator.reconfigure(**{update.key: update.value})
    await broadcast({"type": "config_updated", "data": {update.key: update.value}})
    return {"ok": True, "key": update.key, "value": update.value}

@app.post("/api/resume")
async def resume_trading():
    await _orchestrator.resume()
    await broadcast({"type": "status", "data": _orchestrator.status()})
    return {"ok": True}

@app.get("/api/trades")
async def get_trades(limit: int = 100):
    return await _db.get_recent_trades(limit)

@app.get("/api/performance")
async def get_performance(days: int = 30):
    return await _db.get_daily_performance(days)

@app.get("/api/positions")
async def get_positions() -> list[dict[str, Any]]:
    if _orchestrator:
        return _orchestrator.open_positions()
    return []

@app.get("/api/health")
async def health():
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}

# Serve frontend build if present
if os.path.exists("./frontend/dist"):
    app.mount("/", StaticFiles(directory="./frontend/dist", html=True), name="static")

