"""
Wiring tests for the v9 crash-replay stress simulator.

stress_simulator.py could replay historical crises against an allocation,
but nothing ever called it — a reallocation could go out having been
checked only against the period its own attribution data covered.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.strategies.capital_allocator import AllocationResult


def _strategy(strategy_id: str):
    strategy = MagicMock()
    strategy.strategy_id = strategy_id
    return strategy


def _patched(allocation: dict[str, float], strategies: list[str], floor: float = 0.30):
    """Patch the endpoint's three collaborators: registry, kill switch, allocator."""
    registry = MagicMock()
    registry.all.return_value = [_strategy(s) for s in strategies]
    settings = MagicMock()
    settings.risk.capital_preservation_max_drawdown_pct = floor
    kill_switch = MagicMock()
    kill_switch.enabled_ids.side_effect = lambda ids: set(ids)
    return (
        patch("src.api.main.get_default_registry", return_value=registry),
        patch("src.api.main.get_settings", return_value=settings),
        patch("src.api.main.get_strategy_kill_switch_manager", return_value=kill_switch),
        patch(
            "src.api.main.performance_weighted_allocate",
            return_value=AllocationResult(fractions=allocation, method="performance_weighted"),
        ),
    )


async def _call(allocation, strategies, floor=0.30):
    from src.api.main import get_allocation_stress_test

    a, b, c, d = _patched(allocation, strategies, floor)
    with a, b, c, d:
        return await get_allocation_stress_test()


class TestStressTestEndpoint:
    @pytest.mark.asyncio
    async def test_no_registered_strategies_reports_nothing_to_stress(self) -> None:
        result = await _call({}, [])
        assert result["scenarios"] == []
        assert result["breaches_any_floor"] is False

    @pytest.mark.asyncio
    async def test_every_known_scenario_is_replayed(self) -> None:
        from src.tuning.stress_simulator import KNOWN_CRISIS_SCENARIOS

        result = await _call({"signal_engine_v1": 1.0}, ["signal_engine_v1"])
        names = {s["scenario"] for s in result["scenarios"]}
        assert names == set(KNOWN_CRISIS_SCENARIOS)

    @pytest.mark.asyncio
    async def test_a_fully_allocated_book_breaches_the_default_floor(self) -> None:
        """The 2018 sequence compounds well past a 30% drawdown at full weight."""
        result = await _call({"signal_engine_v1": 1.0}, ["signal_engine_v1"], floor=0.30)
        assert result["breaches_any_floor"] is True

    @pytest.mark.asyncio
    async def test_a_lightly_allocated_book_survives(self) -> None:
        result = await _call({"signal_engine_v1": 0.05}, ["signal_engine_v1"], floor=0.30)
        assert result["breaches_any_floor"] is False

    @pytest.mark.asyncio
    async def test_the_floor_comes_from_config_not_the_module_default(self) -> None:
        """
        breaches_floor must mean "breaches the halt that is actually armed",
        not a number that happens to match the simulator's default today.
        """
        allocation = {"signal_engine_v1": 1.0}
        lenient = await _call(allocation, ["signal_engine_v1"], floor=0.99)
        strict = await _call(allocation, ["signal_engine_v1"], floor=0.01)
        assert lenient["breaches_any_floor"] is False
        assert strict["breaches_any_floor"] is True
        assert lenient["capital_preservation_floor_pct"] == 0.99

    @pytest.mark.asyncio
    async def test_every_strategy_is_replayed_against_the_same_crash(self) -> None:
        """
        Assuming a strategy hedges a crash it has never traded through would
        understate exactly the tail risk this test exists to find.
        """
        solo = await _call({"a": 1.0}, ["a"])
        split = await _call({"a": 0.5, "b": 0.5}, ["a", "b"])
        solo_dd = {s["scenario"]: s["max_drawdown_pct"] for s in solo["scenarios"]}
        split_dd = {s["scenario"]: s["max_drawdown_pct"] for s in split["scenarios"]}
        # Same total weight against the same sequence -> same drawdown. Splitting
        # capital across strategies is not diversification against a market-wide
        # crash, and the report must not imply that it is.
        assert solo_dd == pytest.approx(split_dd)

    @pytest.mark.asyncio
    async def test_kill_switched_strategies_are_excluded_from_the_stress(self) -> None:
        from src.api.main import get_allocation_stress_test

        registry = MagicMock()
        registry.all.return_value = [_strategy("a"), _strategy("b")]
        settings = MagicMock()
        settings.risk.capital_preservation_max_drawdown_pct = 0.30
        kill_switch = MagicMock()
        kill_switch.enabled_ids.return_value = {"a"}
        allocate = MagicMock(
            return_value=AllocationResult(fractions={"a": 1.0, "b": 0.0}, method="x")
        )
        with (
            patch("src.api.main.get_default_registry", return_value=registry),
            patch("src.api.main.get_settings", return_value=settings),
            patch("src.api.main.get_strategy_kill_switch_manager", return_value=kill_switch),
            patch("src.api.main.performance_weighted_allocate", allocate),
        ):
            result = await get_allocation_stress_test()
        assert allocate.call_args.args[1] == {"a"}
        assert result["allocations"] == {"a": 1.0, "b": 0.0}

    @pytest.mark.asyncio
    async def test_drawdown_and_return_are_reported_per_scenario(self) -> None:
        result = await _call({"signal_engine_v1": 1.0}, ["signal_engine_v1"])
        for scenario in result["scenarios"]:
            assert scenario["max_drawdown_pct"] > 0.0
            assert "final_return_pct" in scenario
            assert isinstance(scenario["breaches_floor"], bool)
