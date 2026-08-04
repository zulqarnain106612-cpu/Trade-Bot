"""
CryptoBoxSignalAdapter — bridges the Crypto-Box engine orchestrator
into Trade-Bot's existing signal pipeline.

Activated when the `CRYPTO_BOX` environment variable is set to `true`.
Falls back silently when not enabled, so the existing pipeline is unaffected.

Usage in orchestrator.py:
  adapter = CryptoBoxSignalAdapter()
  signal = await adapter.get_signal(symbol, data)
  # signal.direction, signal.kelly_multiplier, signal.regime are available
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from src.engines.signal_gate import TradeSignal


_ENABLED = os.environ.get("CRYPTO_BOX", "").lower() in ("1", "true", "yes")


class CryptoBoxSignalAdapter:
    """Thin wrapper that runs the full Crypto-Box engine cycle on demand."""

    def __init__(self) -> None:
        self._orchestrator: object | None = None
        if _ENABLED:
            self._init_orchestrator()

    def _init_orchestrator(self) -> None:
        try:
            from src.engines.orchestrator import EngineOrchestrator

            self._orchestrator = EngineOrchestrator()
            log.info("crypto_box_adapter_enabled")
        except Exception as exc:
            log.warning("crypto_box_adapter_init_failed", exc=str(exc))

    @property
    def enabled(self) -> bool:
        return _ENABLED and self._orchestrator is not None

    async def get_signal(self, symbol: str, data: dict[str, Any]) -> TradeSignal | None:
        """
        Run all 18 engines and return a TradeSignal.

        Returns None when Crypto-Box is disabled or if the orchestrator errors.
        Callers must treat None as "no augmentation" and fall back to the
        existing signal pipeline.
        """
        if not self.enabled or self._orchestrator is None:
            return None

        try:
            result = await self._orchestrator.run(symbol, data)  # type: ignore[union-attr]
            return result.trade_signal
        except Exception as exc:
            log.warning("crypto_box_signal_error", symbol=symbol, exc=str(exc))
            return None
