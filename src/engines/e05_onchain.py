"""
E-05 — On-Chain Graph / Wallet Flows engine.

Gap G-12 fix: no local blockchain node.
Uses DeFi Llama TVL flows + CoinGecko supply distribution (both free).
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from src.engines.schema import EngineOutput

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-05"
_SLA_SECONDS = 5
_FLOW_THRESHOLD = 0.02  # 2% TVL change triggers directional signal


class E05OnChain:
    def __init__(self, horizon_hours: int = 24) -> None:
        self._horizon = horizon_hours

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        spot: float = data.get("spot", 0.0)
        if spot <= 0:
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, "no_spot")

        try:
            net_flow = await self._compute_net_flow(symbol, data)
            direction = self._flow_to_direction(net_flow)
            confidence = float(min(abs(net_flow), 1.0))

            # Net flow biases predicted price
            predicted = spot * (1 + net_flow * 0.005)

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=predicted,
                confidence=confidence,
                direction=direction,
                horizon_hours=self._horizon,
                metadata={"net_flow_normalized": net_flow},
            )
        except Exception as exc:
            log.warning("e05_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))

    async def _compute_net_flow(self, symbol: str, data: dict) -> float:
        # Try cached on-chain data first
        onchain = data.get("onchain")
        if onchain and "tvl_24h_change_pct" in onchain:
            return float(onchain["tvl_24h_change_pct"])

        # Fallback: pull from existing DeFi Llama provider
        try:
            from src.intelligence.onchain.defillama_provider import DeFiLlamaProvider

            provider = DeFiLlamaProvider()
            metrics = await provider.fetch_metrics()
            if metrics and isinstance(metrics, dict) and "tvl" in metrics:
                # Derive 24h change from two readings if available
                return float(metrics.get("tvl_change_pct", 0.0))
        except Exception as exc:
            # Fallback provider is best-effort; a neutral 0.0 is returned so the
            # engine stays live, but the failure must not be invisible.
            log.warning("defillama_tvl_fallback_failed", exc=str(exc))
        return 0.0

    @staticmethod
    def _flow_to_direction(net_flow: float) -> int:
        if net_flow > _FLOW_THRESHOLD:
            return 1  # accumulation
        if net_flow < -_FLOW_THRESHOLD:
            return -1  # distribution
        return 0
