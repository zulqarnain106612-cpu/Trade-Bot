"""
Technical indicator library. Every function is a pure transform of past
OHLCV data only — no look-ahead. Formulas match ARCHITECTURE.md section 2.3.
"""

import numpy as np
import pandas as pd


def sma(close: pd.Series, n: int) -> pd.Series:
    return close.rolling(n).mean()


def ema(close: pd.Series, n: int) -> pd.Series:
    return close.ewm(span=n, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(n).mean()
    avg_loss = loss.rolling(n).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def bollinger_bands(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    std = close.rolling(n).std()
    upper = mid + k * std
    lower = mid - k * std
    return upper, mid, lower


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(n).mean()


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff()).fillna(0)
    return (direction * volume).cumsum()


def build_feature_matrix(df: pd.DataFrame) -> pd.DataFrame:
    """
    df must have columns: open, high, low, close, volume.
    Returns a feature matrix aligned to df's index, with NaNs in the
    warm-up period (expected — those rows must be dropped before training,
    not filled, to avoid injecting fabricated information).
    """
    feats = pd.DataFrame(index=df.index)
    feats["sma_20"] = sma(df["close"], 20)
    feats["ema_12"] = ema(df["close"], 12)
    feats["ema_26"] = ema(df["close"], 26)
    macd_line, macd_signal, macd_hist = macd(df["close"])
    feats["macd"] = macd_line
    feats["macd_signal"] = macd_signal
    feats["macd_hist"] = macd_hist
    feats["rsi_14"] = rsi(df["close"])
    bb_up, bb_mid, bb_low = bollinger_bands(df["close"])
    feats["bb_width"] = (bb_up - bb_low) / bb_mid
    feats["bb_pctb"] = (df["close"] - bb_low) / (bb_up - bb_low)
    feats["atr_14"] = atr(df["high"], df["low"], df["close"])
    feats["obv"] = obv(df["close"], df["volume"])
    feats["obv_pct_change"] = feats["obv"].pct_change()
    feats["returns_1"] = df["close"].pct_change(1)
    feats["returns_5"] = df["close"].pct_change(5)
    feats["volume_zscore"] = (df["volume"] - df["volume"].rolling(20).mean()) / df[
        "volume"
    ].rolling(20).std()
    return feats


if __name__ == "__main__":
    from data_fetch_kraken import fetch_ohlcv

    df = fetch_ohlcv("XBTUSD", "1h")
    feats = build_feature_matrix(df)
    print(feats.tail(5))
    print(f"\nWarm-up NaN rows to drop: {feats.isna().any(axis=1).sum()} of {len(feats)}")
