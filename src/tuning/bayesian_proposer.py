"""
Bayesian-optimization proposer for the self-tuning subsystem (Optuna/TPE).

Design: docs/SELF_TUNING_DESIGN.md §2 ("A more sophisticated search
strategy (Bayesian optimization, etc.) can replace TuningProposer.propose
later without touching the safety machinery" -- src/tuning/proposer.py).

This class satisfies the same `Proposer` protocol as `TuningProposer`: it
proposes exactly ONE bounded challenger value per call and never mutates
the champion, registry, or version store. Unlike the random-walk proposer
it is not memoryless -- each call rebuilds an Optuna study from this
parameter's own audit-log history (PROPOSED/EVALUATED pairs) and asks the
TPE sampler for the next point, so proposals converge toward values that
previously improved `primary_metric` instead of wandering uniformly. All
downstream safety (significance testing, gate, shadow mode, watchdog) is
unchanged -- a challenger this proposer picks is exactly as easy to reject
as one TuningProposer picks.
"""

from __future__ import annotations

import optuna
from optuna.distributions import FloatDistribution
from optuna.samplers import TPESampler
from optuna.trial import TrialState, create_trial

from src.tuning.audit import TuningAuditLog, TuningEventType
from src.tuning.proposer import Proposal
from src.tuning.registry import TunableParameter

optuna.logging.set_verbosity(optuna.logging.WARNING)


class BayesianProposer:
    """
    Proposes a challenger value via Optuna's TPE sampler, seeded from this
    parameter's own PROPOSED/EVALUATED audit-log history.

    Falls back to a uniform-random point within [floor, ceiling] when there
    is not yet enough history (`n_startup_trials`, matching Optuna's own
    TPE cold-start behavior) -- functionally equivalent to a wide random
    walk until real evaluation data exists to condition on.
    """

    def __init__(
        self,
        audit_log: TuningAuditLog,
        n_startup_trials: int = 5,
        seed: int | None = None,
    ) -> None:
        self._audit_log = audit_log
        self._n_startup_trials = n_startup_trials
        self._seed = seed

    def _history(
        self, param_name: str, primary_metric: str, floor: float, ceiling: float
    ) -> list[tuple[float, float]]:
        """Pair each PROPOSED entry with the delta of `primary_metric` from
        the EVALUATED entry immediately following it in the audit log."""
        entries = self._audit_log.read_for_param(param_name)
        pairs: list[tuple[float, float]] = []
        pending_value: float | None = None
        for entry in entries:
            if entry.event_type == TuningEventType.PROPOSED:
                pending_value = entry.details.get("challenger_value")
            elif entry.event_type == TuningEventType.EVALUATED and pending_value is not None:
                for comparison in entry.details.get("comparisons", []):
                    if comparison.get("metric") == primary_metric:
                        value = min(ceiling, max(floor, float(pending_value)))
                        pairs.append((value, float(comparison["delta"])))
                        break
                pending_value = None
        return pairs

    def propose(self, param: TunableParameter, primary_metric: str = "") -> Proposal:
        floor, ceiling = param.floor, param.ceiling
        distribution = FloatDistribution(floor, ceiling)
        history = (
            self._history(param.name, primary_metric, floor, ceiling) if primary_metric else []
        )

        study = optuna.create_study(
            direction="maximize",
            sampler=TPESampler(seed=self._seed, n_startup_trials=self._n_startup_trials),
        )
        for value, objective in history:
            study.add_trial(
                create_trial(
                    state=TrialState.COMPLETE,
                    params={"value": value},
                    distributions={"value": distribution},
                    value=objective,
                )
            )

        trial = study.ask({"value": distribution})
        challenger_value = min(ceiling, max(floor, trial.params["value"]))
        range_width = ceiling - floor
        step_pct = abs(challenger_value - param.current) / range_width if range_width else 0.0

        return Proposal(
            param_name=param.name,
            champion_value=param.current,
            challenger_value=challenger_value,
            step_pct=step_pct,
        )
