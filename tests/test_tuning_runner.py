import random
from pathlib import Path

import pytest

from src.config import SelfTuningSettings
from src.tuning.audit import TuningAuditLog, TuningEventType
from src.tuning.evaluator import MetricComparison
from src.tuning.gate import PromotionGate
from src.tuning.proposer import TuningProposer
from src.tuning.registry import ParameterRegistry, TunableParameter
from src.tuning.runner import TuningRunner
from src.tuning.store import VersionedConfigStore


def make_param() -> TunableParameter:
    return TunableParameter(
        name="hmm.entropy_threshold",
        description="test",
        floor=0.3,
        ceiling=0.7,
        current=0.5,
        eval_strategy="cpcv_oos_sharpe",
    )


def build_runner(
    tmp_path: Path, enabled: bool = True, shadow_mode: bool = True, step_pct: float = 0.2
) -> tuple[TuningRunner, ParameterRegistry, VersionedConfigStore, TuningAuditLog]:
    registry = ParameterRegistry()
    registry.register(make_param())
    store = VersionedConfigStore(tmp_path / "versions.jsonl")
    audit = TuningAuditLog(tmp_path / "audit.jsonl")
    settings = SelfTuningSettings(enabled=enabled, min_hours_between_attempts=24.0)
    proposer = TuningProposer(step_pct=step_pct, rng=random.Random(1))
    gate = PromotionGate()
    runner = TuningRunner(registry, store, audit, settings, proposer, gate, shadow_mode=shadow_mode)
    return runner, registry, store, audit


def improving_comparisons() -> list[MetricComparison]:
    return [
        MetricComparison(
            metric_name="oos_sharpe",
            champion_mean=0.01,
            challenger_mean=0.05,
            delta=0.04,
            p_value=0.001,
            significant_improvement=True,
            significant_regression=False,
        )
    ]


def regressing_comparisons() -> list[MetricComparison]:
    return [
        MetricComparison(
            metric_name="oos_sharpe",
            champion_mean=0.05,
            challenger_mean=0.05,
            delta=0.0,
            p_value=0.9,
            significant_improvement=False,
            significant_regression=False,
        ),
        MetricComparison(
            metric_name="max_drawdown_inverted",
            champion_mean=0.9,
            challenger_mean=0.6,
            delta=-0.3,
            p_value=0.001,
            significant_improvement=False,
            significant_regression=True,
        ),
    ]


def test_disabled_kill_switch_skips_everything(tmp_path: Path) -> None:
    runner, _, store, audit = build_runner(tmp_path, enabled=False)
    result = runner.attempt("hmm.entropy_threshold", improving_comparisons(), "oos_sharpe")
    assert not result.attempted
    assert not store.has_versions("hmm.entropy_threshold")
    events = [e.event_type for e in audit.read_all()]
    assert events == [TuningEventType.SKIPPED]


def test_shadow_mode_never_promotes_even_on_accept(tmp_path: Path) -> None:
    runner, _, store, audit = build_runner(tmp_path, shadow_mode=True)
    result = runner.attempt("hmm.entropy_threshold", improving_comparisons(), "oos_sharpe")
    assert result.attempted
    assert result.accepted
    assert not result.promoted
    assert not store.has_versions("hmm.entropy_threshold")
    events = [e.event_type for e in audit.read_all()]
    assert TuningEventType.WOULD_PROMOTE in events
    assert TuningEventType.PROMOTED not in events


def test_live_mode_promotes_on_accept(tmp_path: Path) -> None:
    runner, _, store, audit = build_runner(tmp_path, shadow_mode=False)
    result = runner.attempt("hmm.entropy_threshold", improving_comparisons(), "oos_sharpe")
    assert result.promoted
    assert store.has_versions("hmm.entropy_threshold")
    events = [e.event_type for e in audit.read_all()]
    assert TuningEventType.PROMOTED in events


def test_regression_is_rejected_and_never_promoted(tmp_path: Path) -> None:
    runner, _, store, audit = build_runner(tmp_path, shadow_mode=False)
    result = runner.attempt("hmm.entropy_threshold", regressing_comparisons(), "oos_sharpe")
    assert not result.accepted
    assert not result.promoted
    assert not store.has_versions("hmm.entropy_threshold")
    events = [e.event_type for e in audit.read_all()]
    assert TuningEventType.REJECTED in events


def test_cooldown_blocks_second_attempt(tmp_path: Path) -> None:
    runner, _, store, audit = build_runner(tmp_path, shadow_mode=True)
    runner.attempt("hmm.entropy_threshold", improving_comparisons(), "oos_sharpe")
    result = runner.attempt("hmm.entropy_threshold", improving_comparisons(), "oos_sharpe")
    assert not result.attempted
    assert result.reasons == ("cooldown_active",)


def test_unregistered_parameter_raises(tmp_path: Path) -> None:
    runner, registry, _, _ = build_runner(tmp_path)
    with pytest.raises(KeyError):
        runner.attempt("does.not.exist", improving_comparisons(), "oos_sharpe")
