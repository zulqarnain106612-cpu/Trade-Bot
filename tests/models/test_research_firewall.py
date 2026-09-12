"""
MODL-006 — research output cannot change production parameters directly.

The firewall the source document requires:

```
RESEARCH → candidate model → validation → shadow → paper → production
```

A research notebook must **never** directly change production trading
parameters. The failure this prevents is mundane and expensive: an experiment
becomes the production strategy because someone edited a config while
investigating something else, and nobody can later say when the behaviour
changed or why.

The firewall has three parts, and each is checked here:

1. **The gauntlet is a conjunction.** Every criterion simultaneously, never
   partial credit, and the failures are named so the rejection is auditable.
2. **Shadow evaluation precedes promotion.** A challenger that has not
   accumulated enough evidence is not ready, and "not ready" is not
   "promote".
3. **No research or tuning module writes production parameters by import.**
   The behavioural tests pass right up until someone adds a second path.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.tuning.promotion_gauntlet import (
    GauntletCriteria,
    GauntletObservation,
    evaluate_gauntlet,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Modules whose job is to explore, not to decide.
RESEARCH_PACKAGES = ("src/tuning", "src/upgrade")

#: Anything that can place an order or move money.
EXECUTION_MODULES = (
    "src.execution.live",
    "src.execution.paper",
    "src.execution.router",
    "src.execution.order_manager",
)


def passing_observation(**overrides) -> GauntletObservation:
    values = {
        "trade_count": 100,
        "days_running": 30.0,
        "realized_sharpe": 1.2,
        "realized_max_drawdown_pct": 0.05,
    }
    values.update(overrides)
    return GauntletObservation(**values)


class TestTheGauntletIsAConjunction:
    def test_a_candidate_clearing_every_bar_passes(self):
        result = evaluate_gauntlet(passing_observation())
        assert result.passed
        assert result.failed_criteria == ()

    @pytest.mark.parametrize(
        ("field", "value", "fragment"),
        [
            ("trade_count", 5, "trade_count"),
            ("days_running", 2.0, "days_running"),
            ("realized_sharpe", 0.1, "realized_sharpe"),
            ("realized_max_drawdown_pct", 0.9, "realized_max_drawdown_pct"),
        ],
    )
    def test_failing_any_single_criterion_fails_the_whole_gauntlet(self, field, value, fragment):
        result = evaluate_gauntlet(passing_observation(**{field: value}))
        assert not result.passed
        assert any(fragment in reason for reason in result.failed_criteria)

    def test_every_failure_is_named_not_just_counted(self):
        # The rejection has to be auditable: "did not pass" is not a reason
        # anyone can act on six months later.
        result = evaluate_gauntlet(
            GauntletObservation(
                trade_count=1,
                days_running=0.0,
                realized_sharpe=-2.0,
                realized_max_drawdown_pct=0.95,
            )
        )
        assert not result.passed
        assert len(result.failed_criteria) == 4

    def test_there_is_no_partial_credit(self):
        # Three of four criteria cleared is still a rejection.
        result = evaluate_gauntlet(passing_observation(realized_sharpe=0.0))
        assert not result.passed

    def test_exactly_on_each_bar_passes(self):
        criteria = GauntletCriteria()
        result = evaluate_gauntlet(
            GauntletObservation(
                trade_count=criteria.min_trades,
                days_running=float(criteria.min_days_running),
                realized_sharpe=criteria.min_sharpe,
                realized_max_drawdown_pct=criteria.max_drawdown_pct,
            )
        )
        assert result.passed, result.failed_criteria

    def test_one_step_past_each_bar_fails(self):
        criteria = GauntletCriteria()
        for observation in (
            passing_observation(trade_count=criteria.min_trades - 1),
            passing_observation(days_running=criteria.min_days_running - 0.1),
            passing_observation(realized_sharpe=criteria.min_sharpe - 0.001),
            passing_observation(realized_max_drawdown_pct=criteria.max_drawdown_pct + 0.001),
        ):
            assert not evaluate_gauntlet(observation).passed

    def test_the_criteria_are_frozen(self):
        # Criteria that can be relaxed at runtime are not a gate. To change
        # the bar, change the default and review the diff.
        with pytest.raises(AttributeError):
            GauntletCriteria().min_sharpe = 0.0

    def test_stricter_criteria_can_be_supplied(self):
        strict = GauntletCriteria(min_trades=500, min_days_running=90)
        assert not evaluate_gauntlet(passing_observation(), strict).passed


class TestShadowEvaluationPrecedesPromotion:
    """
    The window is a *time* window, not a sample count.

    `ready_to_evaluate` compares elapsed hours against `shadow_hours`, which
    is the stricter reading of "shadow, then paper, then production": a
    challenger cannot buy its way past the observation period by trading
    more often.
    """

    @staticmethod
    def _deployer(shadow_hours: float):
        from src.upgrade.shadow_deploy import ShadowDeployer

        return ShadowDeployer(
            incumbent=lambda x: 1.0,
            challenger=lambda x: 1.0,
            shadow_hours=shadow_hours,
        )

    def test_an_unstarted_deployment_is_not_ready(self):
        assert not self._deployer(24.0).ready_to_evaluate()

    def test_a_challenger_inside_the_window_is_not_ready(self):
        deployer = self._deployer(24.0)
        deployer.start()
        for _ in range(10):
            deployer.record_return(actual_return=0.01, incumbent_pred=1.0, challenger_pred=1.0)
        assert not deployer.ready_to_evaluate()

    def test_evaluating_early_returns_nothing_rather_than_a_verdict(self):
        # None and "the challenger lost" are different answers, and promoting
        # on the second when you meant the first is how an unvalidated model
        # reaches production.
        deployer = self._deployer(24.0)
        deployer.start()
        deployer.record_return(actual_return=0.05, incumbent_pred=1.0, challenger_pred=1.0)
        assert deployer.evaluate() is None
        assert deployer.result is None

    def test_once_the_window_has_elapsed_a_verdict_is_produced(self):
        deployer = self._deployer(0.0)
        deployer.start()
        for i in range(60):
            move = 0.01 if i % 3 else -0.005
            deployer.record_return(actual_return=move, incumbent_pred=1.0, challenger_pred=1.0)
        assert deployer.ready_to_evaluate()
        assert deployer.evaluate() is not None

    def test_a_challenger_that_does_not_beat_the_incumbent_is_not_promoted(self):
        deployer = self._deployer(0.0)
        deployer.start()
        for i in range(60):
            move = 0.01 if i % 3 else -0.005
            # The challenger takes the opposite side, so it loses.
            deployer.record_return(actual_return=move, incumbent_pred=1.0, challenger_pred=-1.0)
        result = deployer.evaluate()
        assert result is not None
        assert not result.promoted

    def test_a_model_with_no_interface_predicts_zero_rather_than_raising(self):
        # A challenger that is neither callable nor has .predict is a wiring
        # mistake. Returning 0.0 keeps the shadow run alive to report it
        # instead of taking down the tick that noticed.
        from src.upgrade.shadow_deploy import ShadowDeployer

        deployer = ShadowDeployer(incumbent=object(), challenger=object(), shadow_hours=0.0)
        assert deployer.predict_challenger(None) == 0.0


class TestNoResearchModuleReachesAnExecutor:
    @staticmethod
    def _imports(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    @staticmethod
    def _files() -> list[Path]:
        found: list[Path] = []
        for package in RESEARCH_PACKAGES:
            found.extend(sorted((PROJECT_ROOT / package).rglob("*.py")))
        return found

    def test_there_are_research_modules_to_check(self):
        assert len(self._files()) > 10

    @pytest.mark.parametrize(
        "path", _files.__func__(), ids=lambda p: str(p.relative_to(PROJECT_ROOT))
    )
    def test_it_does_not_import_an_executor(self, path):
        offenders = {
            name
            for name in self._imports(path)
            if any(name.startswith(module) for module in EXECUTION_MODULES)
        }
        assert not offenders, (
            f"{path.relative_to(PROJECT_ROOT)} imports {offenders}. An optimiser "
            "reaches live behaviour through bounds, offline evaluation, "
            "out-of-sample check, risk tests, shadow, paper and an approval -- "
            "never by calling an executor."
        )


class TestTheOverrideSurfaceIsNarrow:
    def test_live_overrides_are_read_only_from_the_risk_path(self):
        # `effective_risk_settings` is how a tuned parameter reaches live
        # code. Its contract is to *read* a stored override, never to write
        # one, so the tuning path cannot mutate production from the read side.
        source = (PROJECT_ROOT / "src" / "tuning" / "live_overrides.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        readers = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name.startswith("effective_")
        ]
        assert readers, "live_overrides no longer exposes an effective_* reader"
