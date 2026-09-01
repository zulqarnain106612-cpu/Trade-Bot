"""
Process-wide singletons for the self-tuning subsystem.

Mirrors the pattern of `runtime_config` in src/config.py: module-level
singletons constructed once, imported by both the API layer (Phase 6)
and manual operator scripts (Phase 4). Nothing here registers any
parameter -- that stays an explicit, separate step (src/tuning/bootstrap.py)
so importing this module never silently makes a parameter tunable.
"""

from __future__ import annotations

import asyncio
import threading

from src.config import get_settings
from src.tuning.audit import TuningAuditLog
from src.tuning.bayesian_proposer import BayesianProposer
from src.tuning.gate import PromotionGate
from src.tuning.proposer import Proposer, TuningProposer
from src.tuning.registry import parameter_registry
from src.tuning.runner import TuningRunner
from src.tuning.store import VersionedConfigStore
from src.tuning.watchdog import PostPromotionWatchdog

_settings = get_settings().self_tuning

audit_log: TuningAuditLog = TuningAuditLog(_settings.audit_log_path)
version_store: VersionedConfigStore = VersionedConfigStore(_settings.version_store_path)

# proposer_strategy picks the search strategy only -- gate/evaluator/watchdog
# safety machinery downstream is identical either way (see bayesian_proposer.py).
proposer: Proposer
if _settings.proposer_strategy == "bayesian":
    proposer = BayesianProposer(audit_log)
else:
    proposer = TuningProposer(step_pct=_settings.proposer_step_pct)

gate: PromotionGate = PromotionGate()

# Phase 7: shadow_mode is driven by SelfTuningSettings.shadow_mode (default
# True). Flipping it to False requires an explicit .env edit + restart --
# the same ceremony as TRADING_MODE=live -- never a live API toggle.
runner: TuningRunner = TuningRunner(
    parameter_registry,
    version_store,
    audit_log,
    _settings,
    proposer,
    gate,
    shadow_mode=_settings.shadow_mode,
    decision_log_path=_settings.decision_log_path,
)

watchdog: PostPromotionWatchdog = PostPromotionWatchdog(version_store, audit_log, _settings)


class _PauseState:
    """Runtime, no-restart pause switch (Phase 6), independent of the
    .env-level SELF_TUNING_ENABLED kill switch. An operator can pause via
    the API without needing a restart; resuming likewise takes effect
    immediately for the next attempt() call."""

    def __init__(self) -> None:
        self._paused = False
        # VF-004 pattern (see RuntimeConfig in src/config.py): lazy asyncio.Lock
        # creation without a guard lets two coroutines both see self._lock is
        # None and create separate locks, silently breaking mutual exclusion.
        # Guard the one-time creation with a threading.Lock (cheap; acquired
        # only once per process lifetime).
        self._init_guard: threading.Lock = threading.Lock()
        self._lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is not None:
            return self._lock
        with self._init_guard:
            if self._lock is None:
                self._lock = asyncio.Lock()
        return self._lock

    async def is_paused(self) -> bool:
        async with self._get_lock():
            return self._paused

    async def set_paused(self, value: bool) -> None:
        async with self._get_lock():
            self._paused = value


pause_state: _PauseState = _PauseState()
