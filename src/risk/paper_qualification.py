"""
INV-010, REL-001 — live trading is unlocked by evidence, not by a flag.

The way a bot reaches production is almost never a decision. It is a Tuesday
evening, a backtest that looked good, and `TRADING_MODE=live` in a `.env`
file. Nothing in the process objects, because nothing in the process was ever
asked to.

This module is what gets asked. It holds the qualification criteria as
numbers, evaluates a paper-trading record against all of them, and refuses
live trading until every one has passed. `LiveExecutor.__init__` calls
`assert_qualified_for_live` before it builds any state, alongside the
exchange-key posture check, so there is no path into real money that does not
pass through here.

Three properties are deliberate:

**Every criterion must pass.** Not a score, not a weighted average. A weighted
score lets an exceptional Sharpe ratio buy its way past a drawdown that would
have ended the account, and the drawdown is the one that matters.

**The evidence has a shape.** A record with 3 trades over 2 days cannot
qualify however good those trades were: the sample says nothing, and the most
common way a bot reaches production is on a sample that was never large
enough to fail.

**Failure is itemised.** The result names every criterion that did not pass
and by how much, because "not qualified" with no detail is a message somebody
routes around rather than acts on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

QUALIFICATION_PATH: Final[Path] = Path("data/paper_qualification.json")

SCHEMA_VERSION: Final[int] = 1


class NotQualifiedError(RuntimeError):
    """Live trading was attempted without a passing qualification record."""


@dataclass(frozen=True)
class Criterion:
    """One threshold, with the reasoning that produced the number."""

    name: str
    threshold: float
    higher_is_better: bool
    rationale: str

    def passes(self, measured: float) -> bool:
        return measured >= self.threshold if self.higher_is_better else measured <= self.threshold

    def shortfall(self, measured: float) -> float:
        """How far short the measurement is. Zero or negative means it passed."""
        return self.threshold - measured if self.higher_is_better else measured - self.threshold


# The declared bar. Numbers chosen for this operation and argued for here,
# because a threshold with no reasoning is the one that gets lowered the first
# time it fails.
CRITERIA: Final[tuple[Criterion, ...]] = (
    Criterion(
        "duration_days",
        30.0,
        True,
        "A month covers at least one funding cycle and one weekend regime, and "
        "is the shortest period in which a strategy can be wrong in more than "
        "one way.",
    ),
    Criterion(
        "closed_trades",
        100.0,
        True,
        "Below about a hundred closed trades the win rate and the Sharpe are "
        "noise. This is the criterion that stops a lucky fortnight qualifying.",
    ),
    Criterion(
        "sharpe",
        1.0,
        True,
        "Risk-adjusted return at least as good as the risk taken. Below 1.0 the "
        "strategy is not paying for its own variance.",
    ),
    Criterion(
        "max_drawdown_pct",
        0.20,
        False,
        "A fraction, not a percentage. Past 20% the recovery required is larger "
        "than the loss, and the operator's tolerance usually breaks first.",
    ),
    Criterion(
        "win_rate",
        0.40,
        True,
        "Not a quality measure on its own -- a 35% win rate with large winners "
        "is a fine strategy -- but combined with the profit factor below it "
        "catches the strategy that survives on one outlier.",
    ),
    Criterion(
        "profit_factor",
        1.2,
        True,
        "Gross profit over gross loss. The margin above 1.0 is what absorbs the "
        "slippage and fees that paper trading underestimates.",
    ),
    Criterion(
        "max_slippage_error_bps",
        25.0,
        False,
        "How far the paper fill assumptions were from the observable market. A "
        "strategy qualified on optimistic fills is qualified on fiction.",
    ),
    Criterion(
        "risk_limit_breaches",
        0.0,
        False,
        "Zero. A limit breached in paper is a limit that would have been "
        "breached with money, and the gate exists precisely for the case where "
        "everything else looked good.",
    ),
)


@dataclass(frozen=True)
class PaperRecord:
    """The measured outcome of a paper-trading period."""

    started_at: str
    ended_at: str
    duration_days: float
    closed_trades: int
    sharpe: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    max_slippage_error_bps: float
    risk_limit_breaches: int
    strategy_id: str = "signal_engine_v1"

    def measurement(self, name: str) -> float:
        return float(getattr(self, name))


@dataclass(frozen=True)
class Failure:
    criterion: Criterion
    measured: float

    def __str__(self) -> str:
        direction = "at least" if self.criterion.higher_is_better else "at most"
        return (
            f"{self.criterion.name}: measured {self.measured:g}, "
            f"needs {direction} {self.criterion.threshold:g}"
        )


@dataclass(frozen=True)
class QualificationResult:
    record: PaperRecord
    failures: tuple[Failure, ...] = field(default_factory=tuple)

    @property
    def qualified(self) -> bool:
        return not self.failures

    def report(self) -> str:
        if self.qualified:
            return "qualified: every criterion passed"
        return "not qualified:\n  " + "\n  ".join(str(f) for f in self.failures)


def evaluate(record: PaperRecord) -> QualificationResult:
    """
    Check *record* against every criterion.

    All of them, every time -- no short-circuit on the first failure. An
    operator fixing one problem needs to know about the other three now, not
    a month later when the next attempt fails on the next criterion.
    """
    failures = tuple(
        Failure(c, record.measurement(c.name))
        for c in CRITERIA
        if not c.passes(record.measurement(c.name))
    )
    return QualificationResult(record=record, failures=failures)


def save_qualification(result: QualificationResult, path: Path | None = None) -> None:
    """
    Persist a *passing* result.

    Refuses to write a failing one: a stored "not qualified" record would be
    a thing somebody could later edit into a pass, and the absence of a file
    is a clearer statement than a file saying no.
    """
    if not result.qualified:
        raise NotQualifiedError(result.report())
    target = path or QUALIFICATION_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "qualified_at": datetime.now(tz=UTC).isoformat(),
        "strategy_id": result.record.strategy_id,
        "record": result.record.__dict__,
        "criteria": {c.name: c.threshold for c in CRITERIA},
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_qualification(path: Path | None = None) -> dict[str, Any]:
    target = path or QUALIFICATION_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise NotQualifiedError(
            f"no paper-qualification record at {target}: live trading has not been earned"
        ) from exc
    except ValueError as exc:
        raise NotQualifiedError(f"qualification record is malformed: {target}") from exc
    if not isinstance(raw, dict):
        raise NotQualifiedError(f"qualification record is not an object: {target}")
    return raw


def assert_qualified_for_live(path: Path | None = None) -> None:
    """
    Raise unless a stored record shows every criterion passing *at today's
    thresholds*.

    The re-check against current thresholds is the part that matters. A record
    written when the drawdown limit was 30% must not still unlock live trading
    after the limit is tightened to 20% -- otherwise raising a standard is
    retroactively optional, which is the same as not raising it.
    """
    stored = load_qualification(path)

    if stored.get("schema_version") != SCHEMA_VERSION:
        raise NotQualifiedError(
            f"qualification record schema {stored.get('schema_version')!r} is not "
            f"{SCHEMA_VERSION}; re-qualify rather than migrating the verdict"
        )

    raw_record = stored.get("record")
    if not isinstance(raw_record, dict):
        raise NotQualifiedError("qualification record contains no measurements")

    try:
        record = PaperRecord(**raw_record)
    except TypeError as exc:
        raise NotQualifiedError(f"qualification record is incomplete: {exc}") from exc

    result = evaluate(record)
    if not result.qualified:
        raise NotQualifiedError(
            "the stored qualification no longer meets the current criteria:\n" + result.report()
        )


def criteria_table() -> tuple[Criterion, ...]:
    return CRITERIA
