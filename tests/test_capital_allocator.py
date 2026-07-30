"""Tests for the v2 equal-weight capital allocator."""

from __future__ import annotations

import pytest

from src.strategies.capital_allocator import equal_weight_allocate
from src.strategies.registry import Signal


class _Strat:
    def __init__(self, strategy_id: str, cap: float) -> None:
        self.strategy_id = strategy_id
        self._cap = cap

    def generate_signal(self, bar: object) -> Signal:
        return Signal(0, 0.0, 0.0)

    def required_capital_fraction(self) -> float:
        return self._cap


def test_no_enabled_strategies_all_zero() -> None:
    strategies = (_Strat("a", 0.5), _Strat("b", 0.5))
    result = equal_weight_allocate(strategies, enabled_ids=set())
    assert result.fractions == {"a": 0.0, "b": 0.0}
    assert result.total() == 0.0


def test_equal_split_when_uncapped() -> None:
    strategies = (_Strat("a", 1.0), _Strat("b", 1.0))
    result = equal_weight_allocate(strategies, enabled_ids={"a", "b"})
    assert result.fractions["a"] == pytest.approx(0.5)
    assert result.fractions["b"] == pytest.approx(0.5)
    assert result.total() == pytest.approx(1.0)


def test_disabled_strategy_gets_zero_others_still_allocated() -> None:
    strategies = (_Strat("a", 1.0), _Strat("b", 1.0), _Strat("c", 1.0))
    result = equal_weight_allocate(strategies, enabled_ids={"a", "b"})
    assert result.fractions["c"] == 0.0
    assert result.fractions["a"] == pytest.approx(0.5)
    assert result.fractions["b"] == pytest.approx(0.5)


def test_per_strategy_cap_respected() -> None:
    strategies = (_Strat("a", 0.1), _Strat("b", 1.0))
    result = equal_weight_allocate(strategies, enabled_ids={"a", "b"})
    # a's equal share (0.5) is capped to its own 0.1 ceiling before renorm.
    assert result.fractions["a"] <= 0.1 + 1e-9
    assert result.total() <= 1.0 + 1e-9


def test_total_never_exceeds_one() -> None:
    strategies = (_Strat("a", 1.0), _Strat("b", 1.0), _Strat("c", 1.0), _Strat("d", 1.0))
    result = equal_weight_allocate(strategies, enabled_ids={"a", "b", "c", "d"})
    assert result.total() <= 1.0 + 1e-9


def test_single_enabled_strategy_gets_full_capped_allocation() -> None:
    strategies = (_Strat("a", 0.3), _Strat("b", 1.0))
    result = equal_weight_allocate(strategies, enabled_ids={"a"})
    assert result.fractions["a"] == pytest.approx(0.3)
    assert result.fractions["b"] == 0.0


# ---------------------------------------------------------------------------
# performance_weighted_allocate tests
# ---------------------------------------------------------------------------


class TestPerformanceWeightedAllocate:
    """Verify Sharpe-weighted allocation with warm-up fallback and edge cases."""

    def _strats(self) -> tuple:
        return (_Strat("alpha", 0.5), _Strat("beta", 0.5), _Strat("gamma", 0.5))

    def _seed_tracker(self, fills_by_id: dict) -> None:
        """Inject synthetic fills into the module-level attribution tracker."""
        from src.diagnostics.attribution import AttributedFill, get_attribution_tracker

        tracker = get_attribution_tracker()
        tracker._fills.clear()
        for strategy_id, pnls in fills_by_id.items():
            for i, pnl in enumerate(pnls):
                tracker._fills.append(
                    AttributedFill(strategy_id=strategy_id, pnl_usd=pnl, entry_ts=i, exit_ts=i + 1)
                )

    def _clear_tracker(self) -> None:
        from src.diagnostics.attribution import get_attribution_tracker

        get_attribution_tracker()._fills.clear()

    def test_no_enabled_strategies_all_zero(self) -> None:
        from src.strategies.capital_allocator import performance_weighted_allocate

        strats = self._strats()
        result = performance_weighted_allocate(strats, enabled_ids=set())
        assert all(v == 0.0 for v in result.fractions.values())
        assert result.method == "performance_weighted"

    def test_warmup_falls_back_to_equal_weight_fractions(self) -> None:
        """Strategies with < 30 trades get equal-weight shares."""
        from src.strategies.capital_allocator import performance_weighted_allocate

        self._clear_tracker()
        strats = self._strats()
        result = performance_weighted_allocate(strats, enabled_ids={"alpha", "beta", "gamma"})
        # All in warm-up → equal shares (after cap/renorm with 0.5 caps)
        assert result.total() <= 1.0 + 1e-9
        # All three should be equal (same cap, same warm-up weight)
        assert result.fractions["alpha"] == pytest.approx(result.fractions["beta"], abs=1e-6)

    def test_higher_sharpe_gets_more_capital(self) -> None:
        """Strategy with higher Sharpe should receive more capital than lower."""
        from src.strategies.capital_allocator import performance_weighted_allocate

        # Give alpha 30 trades with high positive Sharpe, beta with lower (noisy) Sharpe.
        # Use varying returns so std != 0 (constant returns give std=0 → Sharpe=0).
        n = 30
        # alpha: tight cluster around +1.0 → high mean/std Sharpe
        alpha_pnls = [1.0 + 0.01 * (i % 3 - 1) for i in range(n)]
        # beta: high variance with modest mean → lower Sharpe
        beta_pnls = [0.5 if i % 2 == 0 else -0.4 for i in range(n)]
        self._seed_tracker({"alpha": alpha_pnls, "beta": beta_pnls})
        strats = (_Strat("alpha", 0.5), _Strat("beta", 0.5))
        result = performance_weighted_allocate(strats, enabled_ids={"alpha", "beta"})
        assert result.fractions["alpha"] > result.fractions["beta"]
        assert result.total() <= 1.0 + 1e-9
        self._clear_tracker()

    def test_negative_sharpe_gets_floor_not_zero(self) -> None:
        """Strategy with negative Sharpe gets floor allocation, not 0."""
        from src.strategies.capital_allocator import performance_weighted_allocate

        n = 30
        # Use varying returns so std != 0 — constant returns give Sharpe=0.
        # alpha: tight cluster around -1.0 → negative Sharpe
        alpha_pnls = [-1.0 + 0.01 * (i % 3 - 1) for i in range(n)]
        # beta: tight cluster around +1.0 → positive Sharpe
        beta_pnls = [1.0 + 0.01 * (i % 3 - 1) for i in range(n)]
        self._seed_tracker({"alpha": alpha_pnls, "beta": beta_pnls})
        strats = (_Strat("alpha", 0.5), _Strat("beta", 0.5))
        result = performance_weighted_allocate(strats, enabled_ids={"alpha", "beta"})
        assert result.fractions["alpha"] > 0.0
        assert result.fractions["beta"] > result.fractions["alpha"]
        self._clear_tracker()

    def test_cap_respected_even_with_extreme_sharpe(self) -> None:
        """required_capital_fraction() cap must hold even for extreme Sharpe."""
        from src.strategies.capital_allocator import performance_weighted_allocate

        n = 30
        self._seed_tracker({"alpha": [100.0] * n})  # absurdly high Sharpe
        strats = (_Strat("alpha", 0.1),)  # capped at 10%
        result = performance_weighted_allocate(strats, enabled_ids={"alpha"})
        assert result.fractions["alpha"] <= 0.1 + 1e-9
        self._clear_tracker()

    def test_result_method_field_is_performance_weighted(self) -> None:
        from src.strategies.capital_allocator import performance_weighted_allocate

        self._clear_tracker()
        strats = self._strats()
        result = performance_weighted_allocate(strats, enabled_ids={"alpha"})
        assert result.method == "performance_weighted"

    def test_metric_sortino_accepted(self) -> None:
        """metric='sortino' must not raise and must produce valid fractions."""
        from src.strategies.capital_allocator import performance_weighted_allocate

        self._clear_tracker()
        result = performance_weighted_allocate(
            self._strats(), enabled_ids={"alpha", "beta"}, metric="sortino"
        )
        assert result.total() <= 1.0 + 1e-9

    def test_metric_calmar_accepted(self) -> None:
        from src.strategies.capital_allocator import performance_weighted_allocate

        self._clear_tracker()
        result = performance_weighted_allocate(
            self._strats(), enabled_ids={"alpha"}, metric="calmar"
        )
        assert result.total() <= 1.0 + 1e-9

    def test_invalid_metric_raises(self) -> None:
        from src.strategies.capital_allocator import performance_weighted_allocate

        with pytest.raises(ValueError, match="metric must be"):
            performance_weighted_allocate(
                self._strats(),
                enabled_ids={"alpha"},
                metric="omega",  # type: ignore[arg-type]
            )

    def test_sortino_metric_with_seeded_data(self) -> None:
        """Sortino-weighted allocation differs from Sharpe-weighted when one
        strategy has high upside vol but similar downside risk."""
        from src.strategies.capital_allocator import performance_weighted_allocate

        n = 30
        # alpha: alternating big gains / tiny losses → high Sortino, moderate Sharpe
        alpha_pnls = [5.0 if i % 2 == 0 else -0.1 for i in range(n)]
        # beta: tight around +1 → similar Sharpe but lower Sortino upside vol
        beta_pnls = [1.0 + 0.05 * (i % 3 - 1) for i in range(n)]
        self._seed_tracker({"alpha": alpha_pnls, "beta": beta_pnls})
        strats = (_Strat("alpha", 0.5), _Strat("beta", 0.5))
        result = performance_weighted_allocate(
            strats, enabled_ids={"alpha", "beta"}, metric="sortino"
        )
        assert result.total() <= 1.0 + 1e-9
        # alpha has much higher Sortino (almost no losses) → should dominate
        assert result.fractions["alpha"] > result.fractions["beta"]
        self._clear_tracker()


# ---------------------------------------------------------------------------
# risk_parity_allocate tests
# ---------------------------------------------------------------------------


class TestRiskParityAllocate:
    """Inverse-volatility weighting: lower vol → higher weight."""

    def _strats(self) -> tuple:
        return (_Strat("a", 0.5), _Strat("b", 0.5))

    def _seed_fills(self, pnls_by_id: dict) -> None:
        from src.diagnostics.attribution import AttributedFill, get_attribution_tracker

        tracker = get_attribution_tracker()
        tracker._fills.clear()
        for sid, pnls in pnls_by_id.items():
            for i, pnl in enumerate(pnls):
                tracker._fills.append(
                    AttributedFill(strategy_id=sid, pnl_usd=pnl, entry_ts=i, exit_ts=i + 1)
                )

    def _clear(self) -> None:
        from src.diagnostics.attribution import get_attribution_tracker

        get_attribution_tracker()._fills.clear()

    def test_no_enabled_strategies_all_zero(self) -> None:
        from src.strategies.capital_allocator import risk_parity_allocate

        strats = self._strats()
        result = risk_parity_allocate(strats, enabled_ids=set())
        assert all(v == 0.0 for v in result.fractions.values())
        assert result.method == "risk_parity"

    def test_method_field(self) -> None:
        from src.strategies.capital_allocator import risk_parity_allocate

        self._clear()
        result = risk_parity_allocate(self._strats(), enabled_ids={"a", "b"})
        assert result.method == "risk_parity"

    def test_warmup_gives_equal_shares(self) -> None:
        """Strategies with < _MIN_TRADES_FOR_VOL fills → equal weight."""
        from src.strategies.capital_allocator import risk_parity_allocate

        self._clear()
        result = risk_parity_allocate(self._strats(), enabled_ids={"a", "b"})
        assert result.total() <= 1.0 + 1e-9
        assert result.fractions["a"] == pytest.approx(result.fractions["b"], abs=1e-6)

    def test_lower_vol_gets_higher_weight(self) -> None:
        """Strategy with tighter P&L distribution should get more capital."""
        from src.strategies.capital_allocator import risk_parity_allocate

        n = 25
        # 'a' has tiny variance (low vol → high inv-vol weight)
        a_pnls = [1.0 + 0.001 * (i % 3) for i in range(n)]
        # 'b' has high variance
        b_pnls = [10.0 if i % 2 == 0 else -9.0 for i in range(n)]
        self._seed_fills({"a": a_pnls, "b": b_pnls})
        result = risk_parity_allocate(self._strats(), enabled_ids={"a", "b"})
        assert result.fractions["a"] > result.fractions["b"]
        assert result.total() <= 1.0 + 1e-9
        self._clear()

    def test_cap_respected(self) -> None:
        """required_capital_fraction() cap must hold even with extreme inv-vol."""
        from src.strategies.capital_allocator import risk_parity_allocate

        n = 25
        self._seed_fills({"a": [0.0001] * n})  # near-zero vol → will hit floor
        strats = (_Strat("a", 0.05),)  # cap 5%
        result = risk_parity_allocate(strats, enabled_ids={"a"})
        assert result.fractions["a"] <= 0.05 + 1e-9
        self._clear()

    def test_total_never_exceeds_one(self) -> None:
        from src.strategies.capital_allocator import risk_parity_allocate

        n = 25
        self._seed_fills({"a": [1.0] * n, "b": [2.0] * n})
        result = risk_parity_allocate(self._strats(), enabled_ids={"a", "b"})
        assert result.total() <= 1.0 + 1e-9
        self._clear()
