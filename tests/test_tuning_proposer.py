import random

import pytest

from src.tuning.proposer import TuningProposer
from src.tuning.registry import TunableParameter


def make_param(current: float = 0.5, floor: float = 0.3, ceiling: float = 0.7) -> TunableParameter:
    return TunableParameter(
        name="hmm.entropy_threshold",
        description="test",
        floor=floor,
        ceiling=ceiling,
        current=current,
        eval_strategy="cpcv_oos_sharpe",
    )


def test_propose_stays_within_bounds() -> None:
    proposer = TuningProposer(step_pct=0.5, rng=random.Random(42))
    param = make_param()
    for _ in range(200):
        proposal = proposer.propose(param)
        assert param.floor <= proposal.challenger_value <= param.ceiling


def test_propose_is_deterministic_with_seeded_rng() -> None:
    param = make_param()
    proposer1 = TuningProposer(step_pct=0.2, rng=random.Random(7))
    proposer2 = TuningProposer(step_pct=0.2, rng=random.Random(7))
    assert proposer1.propose(param).challenger_value == proposer2.propose(param).challenger_value


def test_propose_records_champion_value() -> None:
    proposer = TuningProposer(rng=random.Random(1))
    param = make_param(current=0.55)
    proposal = proposer.propose(param)
    assert proposal.champion_value == 0.55
    assert proposal.param_name == "hmm.entropy_threshold"


def test_invalid_step_pct_rejected() -> None:
    with pytest.raises(ValueError):
        TuningProposer(step_pct=0.0)
    with pytest.raises(ValueError):
        TuningProposer(step_pct=1.5)


def test_propose_clips_at_ceiling() -> None:
    proposer = TuningProposer(step_pct=1.0, rng=random.Random(0))
    param = make_param(current=0.69, floor=0.3, ceiling=0.7)
    for _ in range(50):
        proposal = proposer.propose(param)
        assert proposal.challenger_value <= 0.7


def test_propose_clips_at_floor() -> None:
    proposer = TuningProposer(step_pct=1.0, rng=random.Random(0))
    param = make_param(current=0.31, floor=0.3, ceiling=0.7)
    for _ in range(50):
        proposal = proposer.propose(param)
        assert proposal.challenger_value >= 0.3
