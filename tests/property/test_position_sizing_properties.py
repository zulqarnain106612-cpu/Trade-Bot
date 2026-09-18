"""
RISK-003 — Position size is non-negative, finite and bounded, for any input.

The properties, from the source document's list:

```
position_size >= 0
position_size <= limit
risk <= ceiling
probabilities ∈ [0,1]
invalid inputs never produce executable orders
```

Generation strategy: a seeded `random.Random`, not Hypothesis. The trade-off
is deliberate. Hypothesis gives shrinking, which makes a failure easier to
read; a seeded generator gives a suite that is byte-for-byte reproducible in
CI and adds no dependency to a project whose supply-chain requirements
(`SUP-002`, `SUP-005`) are part of the same programme. The draw pools below
deliberately over-weight the values that break comparisons -- zero, negatives,
denormals, NaN, both infinities, and the exact limits -- which is the part of
Hypothesis's behaviour that actually finds these bugs.

If a case fails, the parameters are in the assertion message; paste them into
a unit test in `tests/risk/` and fix it there. A property test that has caught
something should leave a named regression behind.
"""

from __future__ import annotations

import math
import random

import pytest

from src.strategies.position_sizing import (
    CARVER_FORECAST_SCALAR,
    afml_bet_size,
    carver_forecast_position,
    correlation_adjusted_notional,
    recommend_position_notional,
    thorp_kelly_with_variance,
    vol_target_quantity,
)

#: Fixed so a failure is reproducible. Change it only to hunt for new cases,
#: and record anything it finds as its own test rather than leaving the search
#: to a different seed next time.
SEED = 20260911

#: How many draws per property. Large enough to cover the pathological pools
#: below many times over; small enough that the whole module stays well under
#: a second.
DRAWS = 2_000

#: Values chosen because they break comparisons rather than because they are
#: realistic. Every `x <= 0` guard in the codebase is False for NaN, and
#: `inf` survives most arithmetic intact.
PATHOLOGICAL = [
    0.0,
    -0.0,
    1e-300,
    -1e-300,
    1e300,
    -1e300,
    math.nan,
    math.inf,
    -math.inf,
    -1.0,
    1.0,
]

CAPITALS = [0.0, 1.0, 10.0, 150.0, 10_000.0, 1e6, 1e12, -1.0, *PATHOLOGICAL]
PRICES = [0.0, 1e-12, 0.01, 1.0, 50_000.0, 1e9, *PATHOLOGICAL]
PROBABILITIES = [0.0, 0.5, 1.0, 0.499999, 0.500001, -0.1, 1.1, *PATHOLOGICAL]
RATIOS = [0.0, 0.5, 1.0, 1.5, 100.0, -1.0, *PATHOLOGICAL]
VOLS = [0.0, 1e-9, 1e-6, 0.001, 0.02, 0.5, 10.0, *PATHOLOGICAL]
FORECASTS = [0.0, 1.0, 10.0, 20.0, 25.0, -25.0, 1e9, *PATHOLOGICAL]
CORRELATIONS = [-1.0, 0.0, 0.5, 0.7, 0.7000001, 0.99, 1.0, 1.5, *PATHOLOGICAL]


@pytest.fixture
def rng() -> random.Random:
    return random.Random(SEED)


def _draw(rng: random.Random, pool: list[float]) -> float:
    """Half the time from the pool, half the time a plausible random value."""
    if rng.random() < 0.5:
        return rng.choice(pool)
    return rng.uniform(-1e6, 1e6)


def _ok(value: float, ceiling: float, label: str, args: dict) -> None:
    assert math.isfinite(value), f"{label} returned non-finite {value} for {args}"
    assert value >= 0.0, f"{label} returned negative {value} for {args}"
    assert value <= ceiling + 1e-6, f"{label} returned {value} > ceiling {ceiling} for {args}"


class TestCarverForecastPosition:
    def test_it_is_finite_non_negative_and_capped_at_a_quarter_of_capital(self, rng):
        for _ in range(DRAWS):
            args = {
                "capital_usd": _draw(rng, CAPITALS),
                "forecast": _draw(rng, FORECASTS),
                "daily_vol_pct": _draw(rng, VOLS),
                "price": _draw(rng, PRICES),
            }
            result = carver_forecast_position(**args)
            ceiling = max(0.0, args["capital_usd"] * 0.25)
            if not math.isfinite(ceiling):
                ceiling = math.inf
            _ok(result, ceiling, "carver_forecast_position", args)

    def test_an_instrument_with_no_measurable_risk_refuses(self):
        # price and daily_vol_pct each clear their own guard, but their
        # product is 1e-15 -- so the position would be
        # vol_risk / ~0, i.e. arbitrarily large. The separate
        # instrument-risk guard is what stops it, and it is reachable only
        # through this combination.
        assert (
            carver_forecast_position(
                capital_usd=100_000.0,
                forecast=10.0,
                daily_vol_pct=1e-6,
                price=1e-9,
            )
            == 0.0
        )

    def test_a_zero_or_negative_forecast_scalar_refuses(self):
        for scalar in (0.0, -1.0, -CARVER_FORECAST_SCALAR):
            assert (
                carver_forecast_position(100_000.0, 10.0, 0.02, 50_000.0, forecast_scalar=scalar)
                == 0.0
            )


class TestVolTargetQuantity:
    def test_it_is_finite_and_non_negative(self, rng):
        for _ in range(DRAWS):
            args = {
                "capital_usd": _draw(rng, CAPITALS),
                "price": _draw(rng, PRICES),
                "daily_vol_pct": _draw(rng, VOLS),
            }
            result = vol_target_quantity(**args)
            assert math.isfinite(result), f"non-finite {result} for {args}"
            assert result >= 0.0, f"negative {result} for {args}"


class TestAfmlBetSize:
    def test_it_never_exceeds_max_fraction_of_capital(self, rng):
        for _ in range(DRAWS):
            args = {
                "p_long": _draw(rng, PROBABILITIES),
                "capital_usd": _draw(rng, CAPITALS),
                "max_fraction": rng.choice([0.01, 0.1, 0.25, 1.0]),
            }
            result = afml_bet_size(**args)
            ceiling = max(0.0, args["capital_usd"]) * args["max_fraction"]
            if not math.isfinite(ceiling):
                ceiling = math.inf
            _ok(result, ceiling, "afml_bet_size", args)


class TestThorpKelly:
    def test_it_never_exceeds_the_kelly_ceiling(self, rng):
        for _ in range(DRAWS):
            ceiling_frac = rng.choice([0.01, 0.1, 0.25, 1.0])
            args = {
                "win_prob": _draw(rng, PROBABILITIES),
                "win_loss_ratio": _draw(rng, RATIOS),
                "capital_usd": _draw(rng, CAPITALS),
                "price": _draw(rng, PRICES),
                "kelly_multiplier": rng.choice([0.1, 0.5, 1.0]),
                "kelly_ceiling": ceiling_frac,
                "variance_penalty": _draw(rng, VOLS),
            }
            result = thorp_kelly_with_variance(**args)
            ceiling = max(0.0, args["capital_usd"]) * ceiling_frac
            if not math.isfinite(ceiling):
                ceiling = math.inf
            _ok(result, ceiling, "thorp_kelly_with_variance", args)


class TestCorrelationHaircut:
    def test_it_never_increases_a_position(self, rng):
        for _ in range(DRAWS):
            notional = abs(_draw(rng, CAPITALS))
            correlation = _draw(rng, CORRELATIONS)
            result = correlation_adjusted_notional(notional, correlation)
            assert math.isfinite(result), f"non-finite {result} for {notional}, {correlation}"
            assert result >= 0.0
            if math.isfinite(notional):
                assert result <= notional + 1e-6, (
                    f"haircut increased {notional} to {result} at correlation {correlation}"
                )

    def test_higher_correlation_never_means_a_larger_position(self, rng):
        for _ in range(DRAWS // 4):
            notional = rng.uniform(1.0, 1e6)
            low, high = sorted((rng.uniform(0.0, 1.0), rng.uniform(0.0, 1.0)))
            assert (
                correlation_adjusted_notional(notional, high)
                <= correlation_adjusted_notional(notional, low) + 1e-6
            )


class TestTheCombinedRecommendation:
    def test_every_reported_figure_is_finite_and_non_negative(self, rng):
        for _ in range(DRAWS):
            args = {
                "capital_usd": _draw(rng, CAPITALS),
                "price": _draw(rng, PRICES),
                "p_long": _draw(rng, PROBABILITIES),
                "win_prob": _draw(rng, PROBABILITIES),
                "win_loss_ratio": _draw(rng, RATIOS),
                "forecast": _draw(rng, FORECASTS),
                "daily_vol_pct": _draw(rng, VOLS),
                "avg_book_correlation": _draw(rng, CORRELATIONS),
            }
            sized = recommend_position_notional(**args)
            for key, value in sized.items():
                assert math.isfinite(value), f"{key} was {value} for {args}"
                assert value >= 0.0, f"{key} was {value} for {args}"

    def test_the_recommendation_is_the_correlation_adjusted_minimum_or_zero(self, rng):
        for _ in range(DRAWS // 2):
            args = {
                "capital_usd": rng.uniform(1.0, 1e7),
                "price": rng.uniform(0.01, 1e6),
                "p_long": rng.uniform(0.0, 1.0),
                "win_prob": rng.uniform(0.0, 1.0),
                "win_loss_ratio": rng.uniform(0.0, 10.0),
                "forecast": rng.uniform(-25.0, 25.0),
                "daily_vol_pct": rng.uniform(0.0, 1.0),
                "avg_book_correlation": rng.uniform(-1.0, 1.0),
            }
            sized = recommend_position_notional(**args)
            legs = (sized["thorp_kelly"], sized["afml_bet_size"], sized["carver_forecast"])
            assert sized["correlation_adjusted"] <= min(legs) + 0.01
            if sized["recommended"] > 0.0:
                # Either the adjusted minimum, or the dust floor raising it.
                assert sized["recommended"] >= sized["correlation_adjusted"] - 0.01
                assert sized["recommended"] >= 10.0
