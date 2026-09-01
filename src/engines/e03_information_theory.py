"""
E-03 — Information Theory engine.

Shannon entropy, transfer entropy, and sample entropy on return series.
Direction: entropy alone gives no direction (modulates other engines via
confidence dampening). Used by Depth Detector as entropy feature.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import structlog

from src.engines.schema import EngineOutput

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-03"
_SLA_SECONDS = 3
_WINDOW = 256


def shannon_entropy(returns: np.ndarray, bins: int = 50) -> float:
    counts, _ = np.histogram(returns, bins=bins, density=False)
    total = counts.sum()
    if total == 0:
        return 0.0
    probs = counts / total
    probs = probs[probs > 0]
    return float(-np.sum(probs * np.log2(probs)))


def transfer_entropy(x: np.ndarray, y: np.ndarray, lag: int = 1, bins: int = 10) -> float:
    """TE(X→Y): how much X's past reduces uncertainty of Y's future."""
    n = min(len(x), len(y)) - lag
    if n < 20:
        return 0.0
    y_future = y[lag : lag + n]
    y_present = y[:n]
    x_present = x[:n]

    def _discretize(arr: np.ndarray) -> np.ndarray:
        edges = np.percentile(arr, np.linspace(0, 100, bins + 1))
        edges = np.unique(edges)
        return np.digitize(arr, edges[1:-1])

    yf = _discretize(y_future)
    yp = _discretize(y_present)
    xp = _discretize(x_present)

    def _joint_entropy(*arrs: np.ndarray) -> float:
        combined = np.stack(arrs, axis=1)
        _, counts = np.unique(combined, axis=0, return_counts=True)
        probs = counts / counts.sum()
        return float(-np.sum(probs * np.log2(probs + 1e-12)))

    h_yf_yp = _joint_entropy(yf, yp)
    h_yp = _joint_entropy(yp)
    h_yf_yp_xp = _joint_entropy(yf, yp, xp)
    h_yp_xp = _joint_entropy(yp, xp)
    te = h_yf_yp - h_yp - h_yf_yp_xp + h_yp_xp
    return max(0.0, float(te))


def sample_entropy(series: np.ndarray, m: int = 2, r_factor: float = 0.2) -> float:
    """Regularity measure — lower = more regular/predictable."""
    n = len(series)
    if n < m + 2:
        return 0.0
    r = r_factor * float(np.std(series))
    if r == 0:
        return 0.0

    def _count_matches(template_len: int) -> int:
        count = 0
        for i in range(n - template_len):
            for j in range(i + 1, n - template_len):
                if np.max(np.abs(series[i : i + template_len] - series[j : j + template_len])) < r:
                    count += 1
        return count

    a = _count_matches(m + 1)
    b = _count_matches(m)
    if b == 0:
        return 0.0
    return float(-np.log(a / b)) if a > 0 else float(np.log(b))


class E03InformationTheory:
    def __init__(self, horizon_hours: int = 4) -> None:
        self._horizon = horizon_hours

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        df: pd.DataFrame | None = data.get("ohlcv")
        spot: float = data.get("spot", 0.0)
        if df is None or len(df) < 30 or spot <= 0:
            return EngineOutput.abstain(
                _ENGINE_ID, symbol, spot, self._horizon, "insufficient_data"
            )

        try:
            returns = df["close"].pct_change().dropna().values[-_WINDOW:]
            shan = shannon_entropy(returns)
            max_entropy = np.log2(50)  # max bins
            entropy_score = float(np.clip(shan / max_entropy, 0.0, 1.0))
            predictability_index = 1.0 - entropy_score

            # Transfer entropy BTC→target (if multi-coin data available)
            te_btc_eth = 0.0
            if "btc_returns" in data and data["btc_returns"] is not None:
                te_btc_eth = transfer_entropy(np.array(data["btc_returns"])[-_WINDOW:], returns)

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=spot,  # E-03 modulates, doesn't predict price
                confidence=predictability_index,
                direction=0,  # no directional signal
                horizon_hours=self._horizon,
                metadata={
                    "entropy_score": entropy_score,
                    "predictability_index": predictability_index,
                    "shannon_entropy": shan,
                    "te_btc_eth": te_btc_eth,
                },
            )
        except Exception as exc:
            log.warning("e03_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))
