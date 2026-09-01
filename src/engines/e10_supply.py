"""
E-10 — Protocol Supply / Stock-to-Flow engine.

BTC: S2F PlanB log-linear model.
ETH: PoS emission model (≈0.3% annual supply inflation post-Merge).
LTC: same halving math as BTC, 84M cap, 840k blocks, 2.5min blocks.
Gap G-06 fix: per-coin emission models.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import UTC, datetime

import structlog

from src.engines.schema import EngineOutput

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-10"
_SLA_SECONDS = 2

# PlanB log-linear S2F model coefficients
_S2F_A = 14.6
_S2F_B = 3.3

# BTC constants
_BTC_CAP = 21_000_000
_BTC_HALVING_BLOCKS = 210_000
_BTC_BLOCK_TIME_HOURS = 10 / 60  # ~10 min

# LTC constants
_LTC_CAP = 84_000_000
_LTC_HALVING_BLOCKS = 840_000
_LTC_BLOCK_TIME_HOURS = 2.5 / 60  # 2.5 min

# ETH has no halving schedule; post-Merge circulating supply is ~flat.
_ETH_SUPPLY = 120_500_000.0

# Used only when no block height is supplied. Post-4th-halving values, so the
# fallback lands in the right emission epoch rather than a decade-stale one.
# BlockHeightProvider normally supplies the live height.
_BTC_FALLBACK_HEIGHT = 900_000
_LTC_FALLBACK_HEIGHT = 2_700_000

# Trailing deviation statistics. The engine abstains until _MIN_SAMPLES have
# accumulated rather than emitting a z-score from a near-empty window; the
# history is per-process and resets on restart.
_DEVIATION_WINDOW = 240
_MIN_SAMPLES = 30
_Z_THRESHOLD = 1.0
_Z_SATURATION = 2.5


def _zscore(value: float, history: deque[float]) -> float:
    """Standard score of ``value`` against ``history``. 0.0 when degenerate.

    A constant series does not have exactly zero computed variance — float
    error leaves it around 1e-27 — so an ``== 0`` guard would let the division
    return an arbitrarily large score for a series carrying no information.
    The threshold is therefore relative to the magnitude of the mean.
    """
    n = len(history)
    if n < 2:
        return 0.0
    mean = sum(history) / n
    var = sum((x - mean) ** 2 for x in history) / (n - 1)
    std = math.sqrt(var) if var > 0.0 else 0.0
    if std <= 1e-9 * max(1.0, abs(mean)):
        return 0.0
    return (value - mean) / std


def btc_supply_at_block(height: int) -> float:
    halvings = height // _BTC_HALVING_BLOCKS
    total = 0.0
    for h in range(halvings + 1):
        subsidy = 50.0 / (2**h)
        start = h * _BTC_HALVING_BLOCKS
        end = min((h + 1) * _BTC_HALVING_BLOCKS, height)
        total += subsidy * max(0, end - start)
    return min(total, _BTC_CAP)


def btc_s2f(height: int) -> float:
    supply = btc_supply_at_block(height)
    halvings = height // _BTC_HALVING_BLOCKS
    subsidy = 50.0 / (2**halvings)
    blocks_per_year = 365 * 24 * 60 / 10  # ~52 560 blocks/year at 10-min block time
    annual_new = blocks_per_year * subsidy
    return supply / max(annual_new, 1.0)


def s2f_model_cap(sf: float, a: float = _S2F_A, b: float = _S2F_B) -> float:
    """PlanB's log-linear S2F model. Returns *market capitalisation* in USD.

    exp(14.6) * SF^3.3 is fitted against market value, not unit price — at
    SF≈121 it yields ~1.6e13, which is a plausible cap and a nonsensical
    price. Divide by circulating supply to get a comparable fair value.
    """
    return math.exp(a + b * math.log(max(sf, 0.01)))


def s2f_model_price(sf: float, supply: float, a: float = _S2F_A, b: float = _S2F_B) -> float:
    """Model fair value per coin: S2F market cap divided by circulating supply."""
    return s2f_model_cap(sf, a, b) / max(supply, 1.0)


def ltc_supply_at_block(height: int) -> float:
    halvings = height // _LTC_HALVING_BLOCKS
    total = 0.0
    for h in range(halvings + 1):
        subsidy = 50.0 / (2**h)
        start = h * _LTC_HALVING_BLOCKS
        end = min((h + 1) * _LTC_HALVING_BLOCKS, height)
        total += subsidy * max(0, end - start)
    return min(total, _LTC_CAP)


class E10Supply:
    def __init__(self, horizon_hours: int = 24, window: int = _DEVIATION_WINDOW) -> None:
        self._horizon = horizon_hours
        self._window = window
        # Trailing deviation history per coin, for the z-score below.
        self._history: dict[str, deque[float]] = {}

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        spot: float = data.get("spot", 0.0)
        if spot <= 0:
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, "no_spot")

        coin = symbol.split("/")[0].upper()
        block_height: int = int(data.get("block_height", 0))

        try:
            fair_value, s2f_ratio, cycle_pos, deviation_pct = self._compute(
                coin, spot, block_height
            )

            # The S2F *level* is not tradeable: PlanB's coefficients are fitted
            # on BTC market cap, so the absolute fair value sits far above spot
            # for BTC and is meaningless for ETH/LTC. Comparing spot to it
            # directly pinned direction to +1 on every tick for every coin.
            # What carries information is how today's deviation compares to
            # this coin's own recent deviations.
            hist = self._history.setdefault(coin, deque(maxlen=self._window))
            z = _zscore(deviation_pct, hist)
            hist.append(deviation_pct)

            if len(hist) < _MIN_SAMPLES:
                return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, "warming_up")

            # Deviation unusually high = spot rich against its own norm = short.
            direction = -1 if z > _Z_THRESHOLD else (1 if z < -_Z_THRESHOLD else 0)
            confidence = float(min(1.0, abs(z) / _Z_SATURATION))

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=fair_value,
                confidence=confidence,
                direction=direction,
                horizon_hours=self._horizon,
                metadata={
                    "protocol_fair_value": fair_value,
                    "s2f_ratio": s2f_ratio,
                    "cycle_position": cycle_pos,
                    "deviation_pct": deviation_pct,
                    "deviation_z": z,
                    "samples": len(hist),
                },
            )
        except Exception as exc:
            log.warning("e10_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))

    @staticmethod
    def _compute(coin: str, spot: float, block_height: int) -> tuple[float, float, str, float]:
        if coin == "BTC":
            height = block_height if block_height > 0 else _BTC_FALLBACK_HEIGHT
            sf = btc_s2f(height)
            fair = s2f_model_price(sf, btc_supply_at_block(height))
            cycle_frac = (height % _BTC_HALVING_BLOCKS) / _BTC_HALVING_BLOCKS
        elif coin == "ETH":
            # Post-Merge: PoS emission ~0.3% annually → SF ≈ 333
            sf = 333.0
            fair = s2f_model_price(sf, _ETH_SUPPLY)
            cycle_frac = 0.5  # no halving cycle
        elif coin == "LTC":
            height = block_height if block_height > 0 else _LTC_FALLBACK_HEIGHT
            sf_supply = ltc_supply_at_block(height)
            halvings = height // _LTC_HALVING_BLOCKS
            subsidy = 50.0 / (2**halvings)
            blocks_per_year = 365 * 24 * 60 / 2.5
            annual_new = blocks_per_year * subsidy
            sf = sf_supply / max(annual_new, 1.0)
            fair = s2f_model_price(sf, sf_supply)
            cycle_frac = (height % _LTC_HALVING_BLOCKS) / _LTC_HALVING_BLOCKS
        else:
            return spot, 0.0, "unknown", 0.0

        cycle_pos = "early" if cycle_frac < 0.33 else ("mid" if cycle_frac < 0.66 else "late")
        dev = (spot - fair) / fair * 100
        return fair, sf, cycle_pos, dev
