"""
Model version registry — lightweight metadata store for trained XGBoost models.

Tracks every training event with timestamp, metrics, and file path.
Supports listing versions, comparing across time, and pinning a specific
version for rollback (e.g. when live performance signals model decay).

The registry is file-backed (JSON sidecar per symbol/timeframe) so it
survives process restarts. It never manages the .joblib files themselves;
those are the trainer's responsibility. The registry only records metadata
about them and lets operators select which version is "active".

Usage::

    reg = ModelRegistry(model_dir=Path("models/"))
    reg.register(
        symbol="BTC/USDT",
        timeframe="15m",
        model_type="direction",
        version="20260101_120000",
        file_path=Path("models/xgb_direction_BTC_USDT_15m.joblib"),
        metrics={"oos_sharpe": 1.2, "accuracy": 0.58, "live_gate_pass": True},
    )
    active = reg.active_version("BTC/USDT", "15m", "direction")

Authority:
  Carver (2019) Systematic Trading Ch.12 — model versioning and rollback.
  López de Prado (2018) AFML Ch.11 — model degradation and refresh policy.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_REGISTRY_FILENAME: Final[str] = "model_registry_{symbol}_{timeframe}.json"
_MAX_VERSIONS_PER_KEY: Final[int] = 20  # keep last N versions per (symbol, tf, type)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ModelVersion:
    """Metadata for one trained model version."""

    symbol: str
    timeframe: str
    model_type: str  # "direction" | "meta_label" | "ensemble"
    version: str  # typically ISO timestamp: "20260101_120000"
    file_path: str  # absolute path to the .joblib file
    registered_at: float  # Unix seconds
    metrics: dict[str, Any] = field(default_factory=dict)
    is_pinned: bool = False  # if True, this is the operator-selected active version
    notes: str = ""

    @property
    def live_gate_pass(self) -> bool:
        return bool(self.metrics.get("live_gate_pass", False))

    @property
    def oos_sharpe(self) -> float:
        return float(self.metrics.get("oos_sharpe", 0.0))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["registered_at_iso"] = _ts_to_iso(self.registered_at)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ModelVersion:
        d = dict(d)
        d.pop("registered_at_iso", None)
        return cls(**d)


def _ts_to_iso(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ModelRegistry:
    """
    File-backed model version registry.

    One JSON sidecar per (symbol, timeframe) pair is written to ``model_dir``.
    All versions across model_types are stored in the same sidecar.

    Thread-safety: single-process async loop (same as the rest of the codebase).
    No locks needed.
    """

    def __init__(self, model_dir: Path | str = Path("models")) -> None:
        self._dir = Path(model_dir)
        self._cache: dict[str, list[ModelVersion]] = {}  # key = _cache_key(symbol, tf)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def register(
        self,
        symbol: str,
        timeframe: str,
        model_type: str,
        version: str,
        file_path: Path | str,
        metrics: dict[str, Any] | None = None,
        notes: str = "",
    ) -> ModelVersion:
        """
        Register a newly trained model version.

        If ``live_gate_pass`` is not in metrics and no versions pass the gate,
        the caller should set it explicitly so active_version() can find it.
        """
        mv = ModelVersion(
            symbol=symbol,
            timeframe=timeframe,
            model_type=model_type,
            version=version,
            file_path=str(file_path),
            registered_at=time.time(),
            metrics=dict(metrics or {}),
            notes=notes,
        )
        key = _cache_key(symbol, timeframe)
        versions = self._load(symbol, timeframe)
        versions.append(mv)

        # Prune oldest non-pinned versions beyond cap
        typed = [v for v in versions if v.model_type == model_type]
        if len(typed) > _MAX_VERSIONS_PER_KEY:
            non_pinned = [v for v in typed if not v.is_pinned]
            to_drop = non_pinned[: len(typed) - _MAX_VERSIONS_PER_KEY]
            drop_set = {id(v) for v in to_drop}
            versions = [v for v in versions if id(v) not in drop_set]

        self._cache[key] = versions
        self._save(symbol, timeframe, versions)

        log.info(
            "model_registry.registered",
            symbol=symbol,
            timeframe=timeframe,
            model_type=model_type,
            version=version,
            live_gate=mv.live_gate_pass,
            oos_sharpe=round(mv.oos_sharpe, 3),
        )
        return mv

    def pin(self, symbol: str, timeframe: str, model_type: str, version: str) -> bool:
        """
        Pin a specific version as the active version for this (symbol, tf, type).

        Clears any previous pin for the same key. Returns True if found, False if not.
        """
        versions = self._load(symbol, timeframe)
        found = False
        for v in versions:
            if v.model_type == model_type:
                if v.version == version:
                    v.is_pinned = True
                    found = True
                else:
                    v.is_pinned = False
        if found:
            self._save(symbol, timeframe, versions)
            log.info(
                "model_registry.pinned",
                symbol=symbol,
                timeframe=timeframe,
                model_type=model_type,
                version=version,
            )
        return found

    def unpin(self, symbol: str, timeframe: str, model_type: str) -> None:
        """Remove any operator pin, reverting to latest-gate-passing version."""
        versions = self._load(symbol, timeframe)
        for v in versions:
            if v.model_type == model_type:
                v.is_pinned = False
        self._save(symbol, timeframe, versions)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def active_version(self, symbol: str, timeframe: str, model_type: str) -> ModelVersion | None:
        """
        Return the version that should be loaded.

        Priority:
          1. Operator-pinned version (if present).
          2. Latest version where live_gate_pass=True.
          3. Latest version overall (fallback — operator must accept risk).
        """
        versions = self._load(symbol, timeframe)
        typed = [v for v in versions if v.model_type == model_type]
        if not typed:
            return None

        pinned = [v for v in typed if v.is_pinned]
        if pinned:
            return pinned[-1]

        gate_pass = [v for v in typed if v.live_gate_pass]
        if gate_pass:
            return gate_pass[-1]

        return typed[-1]

    def list_versions(
        self,
        symbol: str,
        timeframe: str,
        model_type: str | None = None,
    ) -> list[ModelVersion]:
        """List all versions for a (symbol, timeframe), optionally filtered by model_type."""
        versions = self._load(symbol, timeframe)
        if model_type is not None:
            versions = [v for v in versions if v.model_type == model_type]
        return sorted(versions, key=lambda v: v.registered_at)

    def summary(self, symbol: str, timeframe: str) -> dict[str, Any]:
        """Summary dict for the /models endpoint."""
        versions = self._load(symbol, timeframe)
        by_type: dict[str, list[dict]] = {}
        for v in versions:
            by_type.setdefault(v.model_type, []).append(v.to_dict())
        active: dict[str, dict | None] = {}
        for mt in {v.model_type for v in versions}:
            av = self.active_version(symbol, timeframe, mt)
            active[mt] = av.to_dict() if av else None
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "n_versions": len(versions),
            "by_type": by_type,
            "active": active,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _registry_path(self, symbol: str, timeframe: str) -> Path:
        safe_symbol = symbol.replace("/", "_")
        filename = _REGISTRY_FILENAME.format(symbol=safe_symbol, timeframe=timeframe)
        return self._dir / filename

    def _load(self, symbol: str, timeframe: str) -> list[ModelVersion]:
        key = _cache_key(symbol, timeframe)
        if key in self._cache:
            return self._cache[key]

        path = self._registry_path(symbol, timeframe)
        if not path.exists():
            self._cache[key] = []
            return []

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            versions = [ModelVersion.from_dict(v) for v in raw.get("versions", [])]
            self._cache[key] = versions
            return versions
        except Exception as exc:
            log.warning("model_registry.load_failed", path=str(path), error=str(exc))
            self._cache[key] = []
            return []

    def _save(self, symbol: str, timeframe: str, versions: list[ModelVersion]) -> None:
        path = self._registry_path(symbol, timeframe)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "symbol": symbol,
                "timeframe": timeframe,
                "versions": [v.to_dict() for v in versions],
            }
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        except Exception as exc:
            log.error("model_registry.save_failed", path=str(path), error=str(exc))


def _cache_key(symbol: str, timeframe: str) -> str:
    return f"{symbol}::{timeframe}"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: ModelRegistry | None = None


def get_registry(model_dir: Path | str | None = None) -> ModelRegistry:
    global _registry
    if _registry is None:
        from src.config import get_settings

        d = Path(model_dir) if model_dir is not None else Path(get_settings().model_dir)
        _registry = ModelRegistry(model_dir=d)
    return _registry
