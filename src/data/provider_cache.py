"""
ProviderCache — process-wide singleton that holds the latest snapshot from
each Crypto-Box background data provider.

Providers (SentimentProvider, MacroProvider, DeribitProvider, OrderbookStream)
write to this cache via their polling loops.  The Crypto-Box adapter reads it
synchronously inside _tick(), avoiding any extra await latency.

Thread-safety: all setters/getters are lock-free because Python's GIL makes
simple dict assignments atomic for the reference swap we do here.
"""

from __future__ import annotations

from typing import Any


class _ProviderCache:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Setters — called by background provider loops
    # ------------------------------------------------------------------

    def set_sentiment(self, fg_score: float, fg_label: str, vader_avg: float) -> None:
        self._data["sentiment"] = {
            "fg_score": fg_score,
            "fg_label": fg_label,
            "vader_compound": vader_avg,
        }

    def set_macro(self, row: dict[str, float]) -> None:
        self._data["macro"] = row

    def set_options(self, symbol_base: str, df: Any) -> None:
        key = f"options_{symbol_base}"
        self._data[key] = df

    def set_orderbook(self, symbol: str, df: Any) -> None:
        self._data[f"orderbook_{symbol}"] = df

    def set_onchain(self, payload: dict[str, Any]) -> None:
        self._data["onchain"] = payload

    def set_exchange_flows(self, flows: list[dict[str, Any]]) -> None:
        self._data["exchange_flows"] = flows

    def set_block_height(self, height: int) -> None:
        self._data["block_height"] = int(height)

    # ------------------------------------------------------------------
    # Getters — called synchronously from _tick()
    # ------------------------------------------------------------------

    def get_sentiment(self) -> dict[str, Any] | None:
        return self._data.get("sentiment")

    def get_macro(self) -> dict[str, float] | None:
        return self._data.get("macro")

    def get_options(self, symbol_base: str) -> Any | None:
        return self._data.get(f"options_{symbol_base}")

    def get_orderbook(self, symbol: str) -> Any | None:
        return self._data.get(f"orderbook_{symbol}")

    def get_onchain(self) -> dict[str, Any] | None:
        return self._data.get("onchain")

    def get_exchange_flows(self) -> list[dict[str, Any]]:
        return self._data.get("exchange_flows", [])

    def get_block_height(self) -> int:
        return int(self._data.get("block_height", 0))

    def snapshot(self, symbol: str) -> dict[str, Any]:
        """Return a complete data snapshot suitable for passing to CryptoBoxSignalAdapter."""
        base = symbol.split("/")[0]
        return {
            "sentiment": self.get_sentiment(),
            "macro": self.get_macro(),
            "options": self.get_options(base),
            "orderbook": self.get_orderbook(symbol),
            "onchain": self.get_onchain(),
            "exchange_flows": self.get_exchange_flows(),
            "block_height": self.get_block_height(),
        }


_cache = _ProviderCache()


def get_provider_cache() -> _ProviderCache:
    """Return the process-wide ProviderCache singleton."""
    return _cache
