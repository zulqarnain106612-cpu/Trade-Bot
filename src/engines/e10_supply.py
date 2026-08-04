"""
E-10 — Protocol Supply / Stock-to-Flow engine.

BTC: S2F PlanB log-linear model.
ETH: PoS emission model (≈0.3% annual supply inflation post-Merge).
LTC: same halving math as BTC, 84M cap, 840k blocks, 2.5min blocks.
Gap G-06 fix: per-coin emission models.
"""

from __future__ import annotations

import math
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


def s2f_model_price(sf: float, a: float = _S2F_A, b: float = _S2F_B) -> float:
    return math.exp(a + b * math.log(max(sf, 0.01)))


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
    def __init__(self, horizon_hours: int = 24) -> None:
        self._horizon = horizon_hours

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
            direction = 1 if spot < fair_value * 0.8 else (-1 if spot > fair_value * 1.2 else 0)
            confidence = float(1 / (1 + abs(deviation_pct / 100)))

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
                },
            )
        except Exception as exc:
            log.warning("e10_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))

    @staticmethod
    def _compute(coin: str, spot: float, block_height: int) -> tuple[float, float, str, float]:
        if coin == "BTC":
            sf = btc_s2f(block_height) if block_height > 0 else 50.0
            fair = s2f_model_price(sf)
            halvings = block_height // _BTC_HALVING_BLOCKS if block_height > 0 else 3
            cycle_frac = (block_height % _BTC_HALVING_BLOCKS) / _BTC_HALVING_BLOCKS
        elif coin == "ETH":
            # Post-Merge: PoS emission ~0.3% annually → SF ≈ 333
            sf = 333.0
            fair = s2f_model_price(sf)
            cycle_frac = 0.5  # no halving cycle
        elif coin == "LTC":
            sf_supply = ltc_supply_at_block(block_height) if block_height > 0 else 70_000_000.0
            halvings = block_height // _LTC_HALVING_BLOCKS if block_height > 0 else 3
            subsidy = 50.0 / (2**halvings)
            blocks_per_year = 365 * 24 * 60 / 2.5
            annual_new = blocks_per_year * subsidy
            sf = sf_supply / max(annual_new, 1.0)
            fair = s2f_model_price(sf)
            cycle_frac = (
                (block_height % _LTC_HALVING_BLOCKS) / _LTC_HALVING_BLOCKS
                if block_height > 0
                else 0.5
            )
        else:
            return spot, 0.0, "unknown", 0.0

        cycle_pos = "early" if cycle_frac < 0.33 else ("mid" if cycle_frac < 0.66 else "late")
        dev = (spot - fair) / fair * 100
        return fair, sf, cycle_pos, dev
