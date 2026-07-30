"""
Real historical OHLCV fetcher — Binance public REST API (no auth required).
No mocks: hits the live public endpoint and returns real historical candles.
"""

import pandas as pd
import requests


BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def fetch_ohlcv(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 1000) -> pd.DataFrame:
    """
    Fetch real historical candles from Binance public API.
    Returns a DataFrame indexed by UTC timestamp with columns:
    open, high, low, close, volume.
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"interval must be one of {list(INTERVAL_MS)}")
    if not (1 <= limit <= 1000):
        raise ValueError("limit must be between 1 and 1000 (Binance API cap)")

    params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
    resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=10)
    resp.raise_for_status()
    raw = resp.json()

    cols = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_asset_volume",
        "trades",
        "taker_buy_base",
        "taker_buy_quote",
        "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)

    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)

    df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
    return df


if __name__ == "__main__":
    df = fetch_ohlcv("BTCUSDT", "1h", 24)
    print(
        f"Fetched {len(df)} real candles, latest close: "
        f"{df['close'].iloc[-1]:.2f} at {df.index[-1]}"
    )
    print(df.tail(3))
