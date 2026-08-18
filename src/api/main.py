"""
Restore src/api/main.py to its pre-instrumentation state.
This file overwrites the current main.py on the default branch to remove
imports and calls that installed diagnostics instrumentation.
"""

# NOTE: This file content was restored to the repo's previous state.
# The original content is preserved; instrumentation was moved to a
# separate diagnostics branch as requested.

from __future__ import annotations

import asyncio
import collections
import hmac  # SCAN3-003: moved from inline import inside set_execution_mode()
import json
import os
import re
import time
from collections.abc import AsyncIterator, Callable, Iterable
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

from src.api.access_control import Permission, Role, require_permission
from src.api.auth import verify_api_key, verify_ws_key
from src.api.metrics import metrics_output
from src.api.middleware import validate_cors_config
from src.config import ExecutionMode, Timeframe, get_settings, runtime_config
from src.data.fetcher import open_fetcher
from src.data.storage import AnyStorageBackend, TradeRecord, create_storage_backend
from src.diagnostics.attribution import get_attribution_tracker
from src.diagnostics.audit_trail import get_audit_trail
from src.diagnostics.disaster_recovery import PositionSnapshot, is_state_consistent, reconcile
from src.engine.orchestrator import Orchestrator
from src.execution.base import AbstractExecutor
from src.execution.unified_ledger import get_unified_ledger
from src.risk.strategy_kill_switch import (
    GauntletNotPassedError,
    get_strategy_kill_switch_manager,
)
from src.strategies.bootstrap import register_default_strategies
from src.strategies.capital_allocator import performance_weighted_allocate
from src.strategies.registry import get_default_registry
from src.tuning.audit import TuningEventType
from src.tuning.meta_allocator import get_allocation_controller
from src.tuning.promotion_gauntlet import (
    GauntletCriteria,
    evaluate_gauntlet,
    observation_from_fills,
)
from src.tuning.scheduler import AutoTuningScheduler
from src.tuning.state import (
    audit_log as tuning_audit_log,
    parameter_registry as tuning_registry,
    pause_state as tuning_pause_state,
    version_store as tuning_version_store,
    watchdog as tuning_watchdog,
)
from src.tuning.store import NoPriorVersionError, NoVersionsError
from src.tuning.stress_simulator import (
    KNOWN_CRISIS_SCENARIOS,
    run_all_known_scenarios,
)


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
    orchestrator: Orchestrator | None
    intel_adapter: Any | None  # IntelligenceAdapter (crypto-intel-v6); None when disabled
    ready: bool  # True only after orchestrator.startup() completes
    _MAX_WS_CLIENTS: int = 50
    _MODE_CHANGE_LIMIT: int = 3
    _MODE_CHANGE_WINDOW_S: float = 3600.0
    _ENDPOINT_LIMIT: int = 60
    _ENDPOINT_WINDOW_S: float = 60.0

    def __init__(self) -> None:
        self.ready = False
        self.intel_adapter = None
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
    # Populate the strategy registry before the orchestrator starts ticking.
    # Registration is not execution — it only makes the enabled strategies
    # visible to capital allocation and attribution — but a fraction config
    # that over-commits the book must fail here, not at the first fill.
    register_default_strategies(cfg=cfg.strategy_portfolio)

    # GAP-006: backend chosen by STORAGE_BACKEND (sqlite | timescale)
    _state.storage = create_storage_backend()
    await _state.storage.initialize()

    async with open_fetcher(_state.storage) as fetcher:
        _state.orchestrator = Orchestrator(_state.storage, fetcher)
        try:
            await _state.orchestrator.startup()
        except Exception as exc:
            # NEW-003: ensure fetcher connections are closed even when startup fails
            log.critical("api.startup_failed", error=str(exc), exc_info=True)
            try:
                await fetcher.close()
            except Exception as close_exc:
                log.warning(
                    "api.fetcher_close_failed_on_startup_error", error=str(close_exc), exc_info=True
                )
            raise
        _state.ready = True  # NEW-001: mark ready only after full startup

        # crypto-intel-v6: start IntelligenceAdapter when INTEL_ENABLED=true
        if os.environ.get("INTEL_ENABLED", "false").lower() == "true":
            try:
                from src.intel import CryptoIntelligence
                from src.intelligence.intelligence_adapter import IntelligenceAdapter

                _intel = CryptoIntelligence()
                _intel.start()
                _state.intel_adapter = IntelligenceAdapter(_intel, _state.storage)
                log.info("api.crypto_intel_v6_started")
            except Exception as _exc:
                log.warning("api.crypto_intel_v6_start_failed", exc=str(_exc))

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
        if _state.intel_adapter is not None:
            try:
                intel_obj = getattr(_state.intel_adapter, "_intel", None)
                if intel_obj is not None:
                    intel_obj.close()
            except Exception as _exc:
                log.warning("api.crypto_intel_close_failed", exc=str(_exc))
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
