"""
RiskGate v2 — CVaR-Kelly sizing + ADWIN per-horizon drift detection.

This is a new risk gate implementation (distinct from existing src/risk/gates.py)
that adds per-horizon ADWIN drift detection and the CVaR-Kelly size formula
from the crypto-intel-v6 spec.

RiskGate.size() formula:
    kelly_raw = edge / (odds * vol)
    kelly     = kelly_raw * kelly_fraction            (half-Kelly)
    cvar_cap  = 0.02 / max(cvar, 1e-6)              (max 2% portfolio CVaR)
    scale     = 1.0 / (1 + horizon_idx * 0.1)       (faster → smaller)
    size      = clip(kelly * min(1, cvar_cap) * scale, 0, 0.05)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass
class SizeResult:
    size_pct: float  # fraction of capital to allocate [0, 0.05]
    kelly_raw: float
    cvar_cap: float
    scale: float
    suppressed: bool  # True if gate blocked (confidence or Sharpe too low)
    reason: str  # human-readable reason for suppression


class RiskGate:
    """
    CVaR-Kelly position sizing with ADWIN drift detection per horizon.

    Used by the IntelligenceAdapter before forwarding signals to execution.
    """

    def __init__(
        self,
        kelly_fraction: float = 0.5,
        conf_threshold: float = 0.65,
        sharpe_min: float = 1.0,
        drawdown_floor: float = 0.10,
        max_daily_loss: float = 0.02,
        n_horizons: int = 10,
        adwin_delta: float = 0.002,
    ) -> None:
        self._kelly_fraction = kelly_fraction
        self._conf_threshold = conf_threshold
        self._sharpe_min = sharpe_min
        self._drawdown_floor = drawdown_floor
        self._max_daily_loss = max_daily_loss
        self._adwin = self._build_adwin(n_horizons, adwin_delta)
        self._n_horizons = n_horizons

    def _build_adwin(self, n: int, delta: float) -> list[Any]:
        try:
            from river.drift import ADWIN  # type: ignore[import]

            return [ADWIN(delta=delta) for _ in range(n)]
        except ImportError:
            log.warning("river_not_installed_adwin_disabled")
            return [None] * n

    def size(
        self,
        signal: dict,
        vol: float,
        cvar: float,
        horizon_idx: int,
    ) -> SizeResult:
        """
        Compute position size as a fraction of capital [0, 0.05].

        signal must have keys: confidence, sharpe_est, edge, odds.
        """
        confidence = float(signal.get("confidence", 0.0))
        sharpe_est = float(signal.get("sharpe_est", 0.0))
        edge = float(signal.get("edge", 0.01))
        odds = float(signal.get("odds", 1.0))

        if confidence < self._conf_threshold:
            return SizeResult(
                0.0, 0.0, 0.0, 0.0, True, f"confidence {confidence:.3f} < {self._conf_threshold}"
            )
        if sharpe_est < self._sharpe_min:
            return SizeResult(
                0.0, 0.0, 0.0, 0.0, True, f"sharpe {sharpe_est:.3f} < {self._sharpe_min}"
            )

        denom = odds * max(vol, 1e-9)
        kelly_raw = edge / denom
        kelly = kelly_raw * self._kelly_fraction
        cvar_cap = 0.02 / max(cvar, 1e-6)
        scale = 1.0 / (1.0 + horizon_idx * 0.1)
        size = float(np.clip(kelly * min(1.0, cvar_cap) * scale, 0.0, 0.05))

        return SizeResult(
            size_pct=size,
            kelly_raw=kelly_raw,
            cvar_cap=cvar_cap,
            scale=scale,
            suppressed=False,
            reason="ok",
        )

    def check_drift(self, horizon_idx: int, metric: float) -> bool:
        """
        Update ADWIN for the given horizon with a new metric observation.

        Returns True when drift is detected → triggers retrain pipeline.
        """
        adwin = self._adwin[horizon_idx]
        if adwin is None:
            return False
        adwin.update(metric)
        drift = bool(adwin.drift_detected)
        if drift:
            log.info("adwin_drift_detected", horizon_idx=horizon_idx, metric=metric)
        return drift

    def circuit_breaker(self, drawdown: float, daily_loss: float) -> bool:
        """
        Return True when drawdown or daily loss exceeds limits.

        Callers must halt all new positions when this returns True.
        """
        if drawdown > self._drawdown_floor:
            log.warning("circuit_breaker_drawdown", drawdown=drawdown, floor=self._drawdown_floor)
            return True
        if daily_loss > self._max_daily_loss:
            log.warning(
                "circuit_breaker_daily_loss", daily_loss=daily_loss, limit=self._max_daily_loss
            )
            return True
        return False

    @classmethod
    def from_config(cls, cfg: dict) -> RiskGate:
        return cls(
            kelly_fraction=cfg.get("kelly_fraction", 0.5),
            conf_threshold=cfg.get("conf_threshold", 0.65),
            sharpe_min=cfg.get("sharpe_min", 1.0),
            drawdown_floor=cfg.get("drawdown_floor", 0.10),
            max_daily_loss=cfg.get("max_daily_loss", 0.02),
            adwin_delta=cfg.get("adwin_delta", 0.002),
        )
