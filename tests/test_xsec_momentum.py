"""Tests for the cross-sectional momentum strategy (v2 Sub-task 2, family 4)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.strategies.registry import StrategyRegistry
from src.strategies.xsec_momentum import (
    CrossSectionalMomentumStrategy,
    UniverseContext,
    rank_universe,
)


def _universe(n: int = 12) -> pd.Series:
    return pd.Series({f"SYM{i}/USDT": float(i) for i in range(n)})


def test_rank_universe_orders_correctly() -> None:
    returns = _universe()
    ranks = rank_universe(returns)
    assert ranks["SYM11/USDT"] == pytest.approx(1.0)
    assert ranks["SYM0/USDT"] == pytest.approx(1.0 / 12)


def test_rejects_non_universecontext_bar() -> None:
    strat = CrossSectionalMomentumStrategy()
    with pytest.raises(TypeError, match="UniverseContext"):
        strat.generate_signal(bar=None)


def test_flat_when_universe_too_small() -> None:
    strat = CrossSectionalMomentumStrategy()
    ctx = UniverseContext(trailing_returns=_universe(5), target_symbol="SYM4/USDT")
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_flat_when_symbol_not_in_universe() -> None:
    strat = CrossSectionalMomentumStrategy()
    ctx = UniverseContext(trailing_returns=_universe(), target_symbol="MISSING/USDT")
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_longs_top_decile_symbol() -> None:
    strat = CrossSectionalMomentumStrategy()
    ctx = UniverseContext(trailing_returns=_universe(), target_symbol="SYM11/USDT")
    sig = strat.generate_signal(ctx)
    assert sig.direction == 1
    assert sig.confidence > 0.0


def test_shorts_bottom_decile_symbol() -> None:
    strat = CrossSectionalMomentumStrategy()
    ctx = UniverseContext(trailing_returns=_universe(), target_symbol="SYM0/USDT")
    sig = strat.generate_signal(ctx)
    assert sig.direction == -1


def test_flat_for_middle_of_pack_symbol() -> None:
    strat = CrossSectionalMomentumStrategy()
    ctx = UniverseContext(trailing_returns=_universe(), target_symbol="SYM6/USDT")
    sig = strat.generate_signal(ctx)
    assert sig.direction == 0


def test_dropna_handles_missing_values_in_universe() -> None:
    returns = _universe()
    returns["NAN/USDT"] = float("nan")
    strat = CrossSectionalMomentumStrategy()
    ctx = UniverseContext(trailing_returns=returns, target_symbol="SYM11/USDT")
    sig = strat.generate_signal(ctx)
    assert sig.direction == 1


def test_registers_with_registry() -> None:
    registry = StrategyRegistry()
    registry.register(CrossSectionalMomentumStrategy())
    assert "xsec_momentum_v1" in registry


def test_rejects_invalid_capital_fraction() -> None:
    with pytest.raises(ValueError, match="max_capital_fraction"):
        CrossSectionalMomentumStrategy(max_capital_fraction=0.0)
