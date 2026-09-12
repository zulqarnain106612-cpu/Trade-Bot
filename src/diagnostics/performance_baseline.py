"""
RES-006 — performance baselines that can fail.

A latency regression does not announce itself. Nothing errors, no alert fires,
and the only symptom is that a decision which used to land inside the bar now
sometimes does not. By the time it is visible as missed fills it has been
there for several releases, and the commit that caused it is long buried.

So the budgets live in `config/performance_baselines.json` and a measured run
is compared against them here. Three properties make this worth having rather
than being a slower test suite:

**Percentiles, not means.** A mean hides exactly the tail that hurts: one
request in a hundred taking two seconds is invisible in an average and fatal
in a trading loop.

**A tolerance, not an equality.** CI runners are noisy shared machines. A
gate that fails on a 3% difference gets disabled within a week, so the
tolerance is 25% -- wide enough to survive the noise, narrow enough that an
accidental O(n^2) cannot hide in it.

**Unknown operations fail.** Measuring something the baselines have never
heard of is not a pass; it means somebody added a hot path and nobody decided
what it may cost.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

BASELINE_PATH: Final[Path] = Path("config/performance_baselines.json")

# Percentiles the baselines declare. p99 is included because the tail is the
# whole point; p50 because a regression that moves the median but not the tail
# is a different (usually worse) kind of change.
PERCENTILES: Final[tuple[int, ...]] = (50, 95, 99)


class BaselineError(RuntimeError):
    """The baseline file is missing, malformed, or silent about an operation."""


@dataclass(frozen=True)
class Baseline:
    operation: str
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_error_rate: float
    max_memory_mb: float
    note: str = ""

    def budget_for(self, percentile: int) -> float:
        return {50: self.p50_ms, 95: self.p95_ms, 99: self.p99_ms}[percentile]


@dataclass(frozen=True)
class Measurement:
    """One measured run of an operation."""

    operation: str
    durations_ms: Sequence[float]
    errors: int = 0
    peak_memory_mb: float = 0.0

    @property
    def samples(self) -> int:
        return len(self.durations_ms)

    @property
    def error_rate(self) -> float:
        total = self.samples + self.errors
        return self.errors / total if total else 0.0

    def percentile(self, p: int) -> float:
        if not self.durations_ms:
            raise BaselineError(f"{self.operation}: no samples to take a percentile of")
        ordered = sorted(self.durations_ms)
        if len(ordered) == 1:
            return ordered[0]
        # Nearest-rank rather than an interpolating estimator: with the small
        # sample counts a CI run produces, interpolation invents a number
        # between two real observations and makes p99 look better than
        # anything actually measured.
        rank = max(1, int(round(p / 100.0 * len(ordered))))
        return ordered[min(rank, len(ordered)) - 1]

    @property
    def mean_ms(self) -> float:
        return statistics.fmean(self.durations_ms) if self.durations_ms else 0.0


@dataclass(frozen=True)
class Regression:
    operation: str
    metric: str
    measured: float
    budget: float

    def __str__(self) -> str:
        return (
            f"{self.operation}.{self.metric}: {self.measured:.2f} exceeds "
            f"budget {self.budget:.2f}"
        )


def load_baselines(path: Path | None = None) -> dict[str, Baseline]:
    """Read the declared budgets, refusing anything malformed."""
    target = path or BASELINE_PATH
    try:
        raw: Any = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BaselineError(f"baseline file is unreadable: {target}") from exc
    except ValueError as exc:
        raise BaselineError(f"baseline file is malformed: {target}") from exc

    operations = raw.get("operations") if isinstance(raw, dict) else None
    if not isinstance(operations, dict) or not operations:
        raise BaselineError(f"baseline file declares no operations: {target}")

    out: dict[str, Baseline] = {}
    for name, spec in operations.items():
        try:
            out[name] = Baseline(
                operation=name,
                p50_ms=float(spec["p50_ms"]),
                p95_ms=float(spec["p95_ms"]),
                p99_ms=float(spec["p99_ms"]),
                max_error_rate=float(spec["max_error_rate"]),
                max_memory_mb=float(spec["max_memory_mb"]),
                note=str(spec.get("note", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BaselineError(f"{name}: incomplete baseline ({exc})") from exc

    for name, baseline in out.items():
        # A baseline whose percentiles are out of order is a typo, and it
        # would silently make the p99 gate looser than the p50 one.
        if not baseline.p50_ms <= baseline.p95_ms <= baseline.p99_ms:
            raise BaselineError(f"{name}: percentile budgets are not ordered")

    return out


def tolerance(path: Path | None = None) -> float:
    target = path or BASELINE_PATH
    raw = json.loads(target.read_text(encoding="utf-8"))
    value = float(raw.get("regression_tolerance", 0.0))
    if not 0.0 <= value < 1.0:
        raise BaselineError(f"regression_tolerance {value!r} is not a sensible fraction")
    return value


def check(
    measurement: Measurement,
    baselines: dict[str, Baseline] | None = None,
    path: Path | None = None,
) -> list[Regression]:
    """
    Compare one measurement against its budget.

    Returns the regressions; an empty list is a pass. Raises `BaselineError`
    for an operation nobody declared a budget for -- silence there would make
    every new hot path free.
    """
    table = baselines if baselines is not None else load_baselines(path)
    baseline = table.get(measurement.operation)
    if baseline is None:
        raise BaselineError(
            f"{measurement.operation!r} has no declared baseline; add one to "
            f"{BASELINE_PATH} rather than measuring it into a void"
        )

    slack = 1.0 + tolerance(path)
    found: list[Regression] = []

    for p in PERCENTILES:
        budget = baseline.budget_for(p) * slack
        measured = measurement.percentile(p)
        if measured > budget:
            found.append(Regression(measurement.operation, f"p{p}_ms", measured, budget))

    if measurement.error_rate > baseline.max_error_rate:
        found.append(
            Regression(
                measurement.operation,
                "error_rate",
                measurement.error_rate,
                baseline.max_error_rate,
            )
        )

    if measurement.peak_memory_mb > baseline.max_memory_mb * slack:
        found.append(
            Regression(
                measurement.operation,
                "peak_memory_mb",
                measurement.peak_memory_mb,
                baseline.max_memory_mb * slack,
            )
        )

    return found


def check_all(
    measurements: Sequence[Measurement], path: Path | None = None
) -> list[Regression]:
    table = load_baselines(path)
    out: list[Regression] = []
    for measurement in measurements:
        out.extend(check(measurement, table, path))
    return out
