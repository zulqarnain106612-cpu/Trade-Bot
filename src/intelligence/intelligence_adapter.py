"""
IntelligenceAdapter — bridges CryptoIntelligence v6 signals into TradeBot's
existing storage schema (store_intelligence_features).

The orchestrator already reads from storage.fetch_intelligence_features() and
joins the columns into the training FeatureMatrix. This adapter calls
CryptoIntelligence.on_bar() for each incoming bar and writes the produced
IntelSignal as intelligence features, making the v6 signals available to every
downstream model, the risk gate, and the position sizer.

Column mapping (18 intelligence columns → IntelSignal fields):
  direction_v6           → signal.direction
  size_pct_v6            → signal.size_pct
  confidence_v6          → signal.confidence
  horizon_idx_v6         → signal.horizon_idx
  ecc_anomaly_v6         → signal.ecc_anomaly
  conflict_v6            → signal.conflict (0/1)
  regime_id_v6           → signal.regime_id
  agreement_ratio_v6     → signal.meta['agreement_ratio']
  kyle_lambda_v6         → signal.meta['kyle_lambda']
  ofi_v6                 → signal.meta['ofi']
  vpin_v6                → signal.meta['vpin']
  algo_ioc_v6            → 1 if algo=='IOC'
  algo_iceberg_v6        → 1 if algo=='iceberg'
  algo_twap_v6           → 1 if algo=='TWAP'
  (4 reserved zeros)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog


if TYPE_CHECKING:
    from src.data.storage import Storage
    from src.intel import CryptoIntelligence

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_TIMEFRAME_MAP: dict[str, int] = {
    "30s": 30_000,
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "7d": 604_800_000,
}


class IntelligenceAdapter:
    """
    Wraps CryptoIntelligence and writes its signals to TradeBot's storage.

    Instantiate alongside the Orchestrator; call on_bar() on each new OHLCV row.
    The Orchestrator's _attach_intelligence_features() will pick up the stored
    rows and join them into the training FeatureMatrix automatically.
    """

    def __init__(self, intelligence: CryptoIntelligence, storage: Storage) -> None:
        self._intel = intelligence
        self._storage = storage

    async def on_bar(
        self,
        symbol: str,
        timeframe: str,
        bar_ts_ms: int,
        ohlcv: dict,
        bids: list | None = None,
        asks: list | None = None,
        regime_id: int = 0,
        regime_confidences: list[float] | None = None,
        derivatives_data: dict | None = None,
        alt_prices: dict[str, list[float]] | None = None,
    ) -> None:
        """
        Process one bar through CryptoIntelligence and persist the signal.

        Does NOT block the caller — IO errors are caught and logged.
        """
        try:
            signal = await self._intel.on_bar(
                symbol=symbol,
                ohlcv=ohlcv,
                bids=bids,
                asks=asks,
                regime_id=regime_id,
                regime_confidences=regime_confidences,
                derivatives_data=derivatives_data,
                alt_prices=alt_prices,
            )
        except Exception as exc:
            log.warning("intelligence_on_bar_failed", symbol=symbol, exc=str(exc))
            return

        if signal is None:
            return

        features = self._signal_to_features(signal)

        try:
            await self._storage.store_intelligence_features(
                symbol=symbol,
                timeframe=timeframe,
                bar_ts=bar_ts_ms,
                features=features,
                confidence=signal.confidence,
                source="crypto_intel_v6",
            )
        except Exception as exc:
            log.warning("intelligence_store_failed", symbol=symbol, exc=str(exc))

    def _signal_to_features(self, signal: Any) -> dict[str, float]:
        """Convert IntelSignal to the 14-column feature dict."""
        meta = getattr(signal, "meta", {})
        return {
            "direction_v6": float(signal.direction),
            "size_pct_v6": float(signal.size_pct),
            "confidence_v6": float(signal.confidence),
            "horizon_idx_v6": float(signal.horizon_idx),
            "ecc_anomaly_v6": float(signal.ecc_anomaly),
            "conflict_v6": float(int(signal.conflict)),
            "regime_id_v6": float(signal.regime_id),
            "agreement_ratio_v6": float(meta.get("agreement_ratio", 0.0)),
            "kyle_lambda_v6": float(meta.get("kyle_lambda", 0.0)),
            "ofi_v6": float(meta.get("ofi", 0.0)),
            "vpin_v6": float(meta.get("vpin", 0.0)),
            "algo_ioc_v6": float(signal.algo == "IOC"),
            "algo_iceberg_v6": float(signal.algo == "iceberg"),
            "algo_twap_v6": float(signal.algo == "TWAP"),
        }
