"""Canonical, validated configuration loader for prediction horizons."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog
import yaml

from src.models.architectures import validate_horizon_architectures

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)
_HORIZONS_CONFIG = Path(os.environ.get("HORIZONS_CONFIG", "config/horizons.yaml"))


def load_horizon_specs(config_path: Path | None = None) -> dict[str, Any]:
    """Load and validate the canonical horizon term structure."""
    path = config_path or _HORIZONS_CONFIG
    try:
        with path.open(encoding="utf-8") as fh:
            document = yaml.safe_load(fh)
        if document is None:
            document = {}
        if not isinstance(document, dict):
            raise ValueError("horizon config root must be a mapping")
        specs = document.get("horizons")
        if specs is None:
            specs = {}
        if not isinstance(specs, dict):
            raise ValueError("horizons must be a mapping")
        seen_ids: set[int] = set()
        for key, spec in specs.items():
            if not isinstance(spec, dict):
                raise ValueError(f"horizon {key!r} must be a mapping")
            horizon_id = int(spec["id"])
            duration = int(spec["seconds"])
            if horizon_id < 0 or horizon_id in seen_ids:
                raise ValueError(f"horizon {key!r} has a negative or duplicate id")
            if duration <= 0:
                raise ValueError(f"horizon {key!r} must have a positive duration")
            if not spec.get("models"):
                raise ValueError(f"horizon {key!r} must declare at least one model")
            model_names = [str(name).strip().lower() for name in spec["models"]]
            unsupported = sorted(set(model_names) - {"mlp"})
            if unsupported or model_names.count("mlp") != 1:
                raise ValueError(
                    f"horizon {key!r} uses architectures without a compatible trainer: "
                    f"{unsupported or model_names}"
                )
            seen_ids.add(horizon_id)
        validate_horizon_architectures({"horizons": specs})
        return specs
    except FileNotFoundError:
        log.warning("horizons_config_not_found", path=str(path))
        return {}
