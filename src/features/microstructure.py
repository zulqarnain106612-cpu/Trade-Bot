"""
Microstructure features: OFI, VPIN, Kyle lambda.

OFI   — Order Flow Imbalance: net signed volume pressure in the order book.
VPIN  — Volume-synchronized Probability of Informed Trading (Easley et al. 2012).
Kyle λ — Price-impact coefficient: Δprice per unit signed order flow.

All functions accept raw trade and order-book data and return float features
suitable for direct injection into the neural ensemble feature bus.
"""

from __future__ import annotations

from collections import deque
from typing import NamedTuple

import numpy as np


# ── Order Flow Imbalance ─────────────────────────────────────────────────────


def compute_ofi(bids: list[list[float]], asks: list[list[float]], depth: int = 5) -> float:
    """
    Order Flow Imbalance ∈ [-1, +1].

    Positive → buy-side pressure; negative → sell-side pressure.
    Uses top-`depth` levels of each side.
    """
    bid_vol = sum(b[1] for b in bids[:depth]) if bids else 0.0
    ask_vol = sum(a[1] for a in asks[:depth]) if asks else 0.0
    total = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return float((bid_vol - ask_vol) / total)


# ── VPIN ─────────────────────────────────────────────────────────────────────


class VPINState(NamedTuple):
    vpin: float  # current VPIN ∈ [0, 1]
    buy_vol: float  # cumulative buy volume in current bucket
    sell_vol: float  # cumulative sell volume in current bucket
    bucket_vol: float  # volume accumulated in current bucket
    buckets: list[float]  # completed bucket imbalances (last n)


def _classify_trade(price: float, prev_price: float, volume: float) -> tuple[float, float]:
    """Tick-rule trade classification → (buy_vol, sell_vol)."""
    if price > prev_price:
        return volume, 0.0
    if price < prev_price:
        return 0.0, volume
    return volume * 0.5, volume * 0.5


class VPINTracker:
    """
    Streaming VPIN tracker using the bulk-volume classification approach.

    Each bucket accumulates `bucket_size` volume; when full, the imbalance
    (|buy - sell| / bucket_size) is recorded.  VPIN = avg over last `n` buckets.
    """

    def __init__(self, bucket_size: float = 1000.0, n_buckets: int = 50) -> None:
        self._bucket_size = bucket_size
        self._n = n_buckets
        self._buy_vol = 0.0
        self._sell_vol = 0.0
        self._bucket_vol = 0.0
        self._buckets: deque[float] = deque(maxlen=n_buckets)
        self._prev_price: float | None = None

    def update(self, price: float, volume: float) -> float:
        """Ingest one trade, return current VPIN."""
        prev = self._prev_price if self._prev_price is not None else price
        buy, sell = _classify_trade(price, prev, volume)
        self._prev_price = price
        remaining = volume
        while remaining > 0:
            space = self._bucket_size - self._bucket_vol
            fill = min(remaining, space)
            frac = fill / volume
            self._buy_vol += buy * frac
            self._sell_vol += sell * frac
            self._bucket_vol += fill
            remaining -= fill
            if self._bucket_vol >= self._bucket_size:
                imbalance = abs(self._buy_vol - self._sell_vol) / self._bucket_size
                self._buckets.append(imbalance)
                self._buy_vol = 0.0
                self._sell_vol = 0.0
                self._bucket_vol = 0.0
        return self.vpin

    @property
    def vpin(self) -> float:
        if not self._buckets:
            return 0.0
        return float(np.mean(self._buckets))


# ── Kyle Lambda ──────────────────────────────────────────────────────────────


class KyleLambdaEstimator:
    """
    Rolling Kyle lambda: price-impact coefficient.

    Estimated via OLS on a rolling window:
        Δprice_t = λ * signed_volume_t + ε_t

    λ > 0 means positive (buying moves price up, selling moves it down).
    Larger λ = lower liquidity / higher market impact per unit volume.
    """

    def __init__(self, window: int = 200) -> None:
        self._prices: deque[float] = deque(maxlen=window + 1)
        self._signed_vols: deque[float] = deque(maxlen=window)
        self._lambda: float = 0.0

    def update(self, price: float, signed_volume: float) -> float:
        """
        Update with new price and signed_volume (+= buy, -= sell).
        Returns current lambda estimate.
        """
        self._prices.append(price)
        if len(self._prices) < 2:
            return self._lambda
        self._signed_vols.append(signed_volume)
        if len(self._signed_vols) < 20:
            return self._lambda
        x = np.array(self._signed_vols, dtype=float)
        prices = list(self._prices)
        y = np.diff(prices[-len(x) - 1 :])
        if len(y) == 0:
            return self._lambda
        xbar, ybar = x.mean(), y.mean()
        cov = float(np.mean((x - xbar) * (y - ybar)))
        var = float(np.var(x))
        if var < 1e-12:
            return self._lambda
        self._lambda = cov / var
        return self._lambda

    @property
    def lambda_(self) -> float:
        return self._lambda


# ── Feature bundle ────────────────────────────────────────────────────────────


class MicrostructureFeatures(NamedTuple):
    ofi: float  # [-1, 1]
    vpin: float  # [0, 1]
    kyle_lambda: float  # price impact coefficient (units: price / volume)


def build_microstructure_features(
    bids: list[list[float]],
    asks: list[list[float]],
    vpin_tracker: VPINTracker,
    kyle_estimator: KyleLambdaEstimator,
    last_price: float,
    last_trade_volume: float,
    last_trade_side: str,  # "buy" or "sell"
) -> MicrostructureFeatures:
    ofi = compute_ofi(bids, asks)
    signed_vol = last_trade_volume if last_trade_side == "buy" else -last_trade_volume
    vpin = vpin_tracker.update(last_price, last_trade_volume)
    kyle = kyle_estimator.update(last_price, signed_vol)
    return MicrostructureFeatures(ofi=ofi, vpin=vpin, kyle_lambda=kyle)
