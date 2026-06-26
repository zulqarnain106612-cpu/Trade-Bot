"""
Crypto intelligence layer — on-chain metrics, exchange flows, whale tracking.

Providers:
  - Glassnode: On-chain flows, whale activity, entity classification
  - CryptoQuant: Exchange-specific flows, leverage, liquidations
  - (Optional) Sentiment: LunarCrush social sentiment integration

Authority:
  - Glassnode API Docs: https://docs.glassnode.com/
  - CryptoQuant API Docs: https://docs.cryptoquant.com/
  - Research: Chainalysis, Coin Metrics on flow analysis
"""

from src.intelligence.client import IntelligenceAggregator
from src.intelligence.metrics import IntelligenceMetrics

__all__ = ["IntelligenceAggregator", "IntelligenceMetrics"]
