"""
Strategy-level correlation layer — v2 Sub-task 3.

Gap-005's PortfolioCorrelationTracker (src/risk/portfolio_correlation.py)
already implements EWM pairwise correlation tracking keyed by an arbitrary
string identifier ("symbol"). Rather than duplicate that machinery, this
module reuses it verbatim, keyed by strategy_id instead of trading symbol,
to answer the v2 question: "is this strategy's realized return stream
correlated with the other strategies currently allocated capital?"

Both correlation ceilings apply multiplicatively at position-sizing time —
asset correlation (Gap-005) and strategy correlation (this module) reduce
size independently; Kelly remains the outer ceiling in all cases (Domain
Prior: Kelly is a ceiling, not a target).

Reference: López de Prado (2018) AFML Ch.16 — portfolio construction across
heterogeneous return streams. Carver (2019) Systematic Trading Ch.11 —
diversification across strategies, not just instruments.
"""

from __future__ import annotations

from src.risk.portfolio_correlation import PortfolioCorrelationTracker


class StrategyCorrelationTracker:
    """
    Thin, strategy-scoped wrapper over PortfolioCorrelationTracker.

    Usage::

        tracker = StrategyCorrelationTracker()
        tracker.push_strategy_returns(
            {"mean_reversion_pairs_v1": 0.001, "breakout_volume_v1": -0.0004}
        )
        scalar = tracker.correlation_scalar(
            new_strategy_id="funding_carry_v1",
            active_strategy_ids=["mean_reversion_pairs_v1"],
        )
    """

    def __init__(self, halflife: int | None = None) -> None:
        self._inner = (
            PortfolioCorrelationTracker(halflife=halflife)
            if halflife is not None
            else PortfolioCorrelationTracker()
        )

    def push_strategy_returns(self, returns_by_strategy_id: dict[str, float]) -> None:
        """Record one bar's realized return per active strategy, atomically."""
        self._inner.push_bar_returns(returns_by_strategy_id)

    def correlation(self, strategy_id_a: str, strategy_id_b: str) -> float | None:
        return self._inner.correlation(strategy_id_a, strategy_id_b)

    def avg_correlation_with_active_strategies(
        self, new_strategy_id: str, active_strategy_ids: list[str]
    ) -> float:
        return self._inner.avg_correlation_with_open_positions(
            new_symbol=new_strategy_id, open_symbols=active_strategy_ids
        )

    def correlation_scalar(
        self,
        new_strategy_id: str,
        active_strategy_ids: list[str],
        threshold: float = 0.60,
    ) -> float:
        """
        Position-size scalar [0, 1] for new_strategy_id given the other
        strategies currently allocated capital. 1.0 = no reduction.
        """
        return self._inner.correlation_scalar(
            new_symbol=new_strategy_id,
            open_symbols=active_strategy_ids,
            threshold=threshold,
        )

    def correlation_matrix(self) -> dict[tuple[str, str], float | None]:
        return self._inner.correlation_matrix()

    @property
    def tracked_strategy_ids(self) -> list[str]:
        return self._inner.tracked_symbols


_strategy_correlation: StrategyCorrelationTracker = StrategyCorrelationTracker()


def get_strategy_correlation() -> StrategyCorrelationTracker:
    """Module-level singleton for the strategy correlation tracker."""
    return _strategy_correlation


def combined_correlation_scalar(
    asset_scalar: float,
    strategy_scalar: float,
) -> float:
    """
    Multiplicative combination of the asset-level (Gap-005) and
    strategy-level correlation scalars. Both are independent ceilings on
    position size — neither one alone should be treated as sufficient.
    """
    if not 0.0 <= asset_scalar <= 1.0:
        raise ValueError(f"asset_scalar must be in [0, 1], got {asset_scalar}")
    if not 0.0 <= strategy_scalar <= 1.0:
        raise ValueError(f"strategy_scalar must be in [0, 1], got {strategy_scalar}")
    return asset_scalar * strategy_scalar
