from src.tuning.evaluator import EvaluationResult, MetricComparison
from src.tuning.gate import PromotionGate
from src.tuning.registry import TunableParameter


def make_param() -> TunableParameter:
    return TunableParameter(
        name="hmm.entropy_threshold",
        description="test",
        floor=0.3,
        ceiling=0.7,
        current=0.5,
        eval_strategy="cpcv_oos_sharpe",
    )


def comparison(name: str, improved: bool = False, regressed: bool = False) -> MetricComparison:
    return MetricComparison(
        metric_name=name,
        champion_mean=0.0,
        challenger_mean=0.0,
        delta=0.0,
        p_value=0.01,
        significant_improvement=improved,
        significant_regression=regressed,
    )


def test_accepts_when_primary_improved_and_no_regression() -> None:
    gate = PromotionGate()
    param = make_param()
    evaluation = EvaluationResult(
        param_name=param.name,
        challenger_value=0.55,
        comparisons=(comparison("oos_sharpe", improved=True), comparison("win_rate")),
    )
    decision = gate.decide(param, evaluation, primary_metric="oos_sharpe")
    assert decision.accepted


def test_rejects_out_of_bounds_challenger() -> None:
    gate = PromotionGate()
    param = make_param()
    evaluation = EvaluationResult(
        param_name=param.name,
        challenger_value=0.9,
        comparisons=(comparison("oos_sharpe", improved=True),),
    )
    decision = gate.decide(param, evaluation, primary_metric="oos_sharpe")
    assert not decision.accepted
    assert any("bounds" in r for r in decision.reasons)


def test_rejects_when_any_metric_regresses_even_if_primary_improves() -> None:
    gate = PromotionGate()
    param = make_param()
    evaluation = EvaluationResult(
        param_name=param.name,
        challenger_value=0.55,
        comparisons=(
            comparison("oos_sharpe", improved=True),
            comparison("max_drawdown_inverted", regressed=True),
        ),
    )
    decision = gate.decide(param, evaluation, primary_metric="oos_sharpe")
    assert not decision.accepted
    assert any("regression" in r for r in decision.reasons)


def test_rejects_when_primary_metric_not_significant() -> None:
    gate = PromotionGate()
    param = make_param()
    evaluation = EvaluationResult(
        param_name=param.name,
        challenger_value=0.55,
        comparisons=(comparison("oos_sharpe", improved=False),),
    )
    decision = gate.decide(param, evaluation, primary_metric="oos_sharpe")
    assert not decision.accepted


def test_rejects_when_primary_metric_missing() -> None:
    gate = PromotionGate()
    param = make_param()
    evaluation = EvaluationResult(param_name=param.name, challenger_value=0.55, comparisons=())
    decision = gate.decide(param, evaluation, primary_metric="oos_sharpe")
    assert not decision.accepted
    assert any("not present" in r for r in decision.reasons)
