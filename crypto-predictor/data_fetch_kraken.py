"""
Real historical OHLCV fetcher — Kraken public REST API (no auth required).
Binance's public API returns HTTP 451 (geo-blocked) from this environment;
Kraken and CoinGecko are reachable and used instead. Swap the base URL if
deploying somewhere Binance is accessible — the DataFrame contract stays
the same either way, so downstream code (indicators/backtester) is unaffected.
"""

import pandas as pd
import requests


KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"

# Kraken interval is in minutes
INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def fetch_ohlcv(pair: str = "XBTUSD", interval: str = "1h") -> pd.DataFrame:
    """
    Fetch real historical candles from Kraken's public API.
    Returns a DataFrame indexed by UTC timestamp with columns:
    open, high, low, close, vwap, volume, trades.
    Kraken returns ~720 most recent candles per call (no arbitrary limit param).
    """
    if interval not in INTERVAL_MINUTES:
        raise ValueError(f"interval must be one of {list(INTERVAL_MINUTES)}")

    params = {"pair": pair.upper(), "interval": INTERVAL_MINUTES[interval]}
    resp = requests.get(KRAKEN_OHLC_URL, params=params, timeout=10)
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("error"):
        raise RuntimeError(f"Kraken API error: {payload['error']}")

    result_key = next(k for k in payload["result"] if k != "last")
    raw = payload["result"][result_key]

    cols = ["time", "open", "high", "low", "close", "vwap", "volume", "trades"]
    df = pd.DataFrame(raw, columns=cols)
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    for c in ["open", "high", "low", "close", "vwap", "volume"]:
        df[c] = df[c].astype(float)

    return df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]


if __name__ == "__main__":
    df = fetch_ohlcv("XBTUSD", "1h")
    print(f"Fetched {len(df)} REAL candles from Kraken (live API, not synthetic)")
    print(f"Latest close: {df['close'].iloc[-1]:.2f} at {df.index[-1]}")
    print(df.tail(3))
