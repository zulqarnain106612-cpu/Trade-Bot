"""
Candidate-value proposer for the self-tuning subsystem.

Design: docs/SELF_TUNING_DESIGN.md §2 ("TuningProposer").

Proposes exactly ONE bounded candidate value per call -- never mutates the
champion, never writes to the registry or the version store. The proposer
is deliberately dumb (bounded random-walk step): the safety of the system
comes from the evaluator + gate downstream (§4-5 of the design doc), not
from the proposer being clever. A more sophisticated search strategy
(Bayesian optimization, etc.) can replace `TuningProposer.propose` later
without touching the safety machinery.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol

from src.tuning.registry import TunableParameter


@dataclass(frozen=True)
class Proposal:
    param_name: str
    champion_value: float
    challenger_value: float
    step_pct: float


class Proposer(Protocol):
    """
    Structural interface TuningRunner depends on -- any proposer strategy
    (random-walk, Bayesian, ...) plugs in here without runner/gate/evaluator
    changes. `primary_metric` is optional so a strategy that doesn't need
    evaluation history (e.g. TuningProposer) can ignore it.
    """

    def propose(self, param: TunableParameter, primary_metric: str = "") -> Proposal: ...


class TuningProposer:
    """
    Proposes a single bounded challenger value via a random-walk step.

    step_pct is the max step size as a fraction of the parameter's
    [floor, ceiling] range, e.g. 0.1 == up to 10% of the range per step.
    A small step size means challengers change behavior incrementally,
    which keeps each evaluation cycle's blast radius small and makes
    causal attribution ("this change caused this effect") tractable.
    """

    def __init__(self, step_pct: float = 0.1, rng: random.Random | None = None) -> None:
        if not (0.0 < step_pct <= 1.0):
            raise ValueError(f"step_pct must be in (0, 1], got {step_pct}")
        self._step_pct = step_pct
        self._rng = rng or random.Random()

    def propose(self, param: TunableParameter, primary_metric: str = "") -> Proposal:
        # primary_metric unused: the random-walk strategy needs no evaluation
        # history. Accepted only so this class satisfies the Proposer protocol.
        range_width = param.ceiling - param.floor
        max_step = range_width * self._step_pct
        # Symmetric step in [-max_step, max_step], clipped to stay in bounds.
        step = self._rng.uniform(-max_step, max_step)
        challenger_value = param.current + step
        challenger_value = max(param.floor, min(param.ceiling, challenger_value))
        return Proposal(
            param_name=param.name,
            champion_value=param.current,
            challenger_value=challenger_value,
            step_pct=self._step_pct,
        )
