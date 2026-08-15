"""
Strategy decay detection — v10 Fully Autonomous Multi-Decade Operation.

Distinguishes "this strategy is in a temporarily unfavorable regime" from
"this strategy's edge has structurally decayed and should be retired,"
using a CUSUM (cumulative sum) test on rolling Sharpe relative to its
promotion-time baseline. A single bad patch triggers v2's kill-switch
(temporary, auto-reversible via re-enable after re-validation); a
CUSUM-confirmed structural break is a stronger, persistent signal that
should route to v6's promotion gauntlet for full re-evaluation before any
re-enable is even considered.

Authority:
  - Page (1954) "Continuous Inspection Schemes" — CUSUM control chart,
    the standard tool for detecting a persistent (not transient) shift in
    a monitored statistic
  - Domain Prior: treat regime transitions as probabilistic; this module
    outputs a continuous CUSUM statistic and a boolean threshold breach,
    never a silent, hard-coded retirement decision
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class CusumDecayDetector:
    """
    One-sided CUSUM detector for downward drift in a rolling performance
    statistic (e.g. per-trade Sharpe contribution) relative to a baseline
    mean. Accumulates evidence over time rather than reacting to any
    single observation — this is what distinguishes it from the
    single-window drift check in performance_drift.py.
    """

    baseline_mean: float
    slack: float = 0.5  # allowance before an observation counts as "below baseline"
    decision_threshold: float = 5.0  # cumulative evidence needed to flag decay
    _cusum: float = field(default=0.0, init=False)
    _observation_count: int = field(default=0, init=False)

    def update(self, observed_value: float) -> float:
        """
        Records one new observation and returns the updated CUSUM
        statistic. A rising, sustained CUSUM (approaching
        decision_threshold) indicates persistent underperformance vs.
        baseline_mean — a single dip resets partially (the max(0, ...)
        floor) so transient noise does not accumulate indefinitely.
        """
        self._observation_count += 1
        deviation = self.baseline_mean - observed_value - self.slack
        self._cusum = max(0.0, self._cusum + deviation)
        return self._cusum

    @property
    def cusum_statistic(self) -> float:
        return self._cusum

    @property
    def is_decayed(self) -> bool:
        return self._cusum >= self.decision_threshold

    @property
    def observation_count(self) -> int:
        return self._observation_count

    def reset(self) -> None:
        """Explicit reset after a strategy has been fully re-validated."""
        self._cusum = 0.0
        self._observation_count = 0
