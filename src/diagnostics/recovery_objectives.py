"""
RES-003, RES-004 — recovery objectives with numbers, and a drill that uses them.

"RTO: minutes. RPO: near-zero." Every disaster-recovery document starts like
that, and none of those phrases can fail. An objective that cannot fail is not
an objective; it is a sentiment. So each one here carries an actual number,
chosen for this operation and justified in the entry itself, and each has a
`measure` path so a drill produces a pass or a fail rather than a feeling.

The numbers are derived from what this bot actually does, not copied from an
enterprise template:

  * **RPO for trade records is zero.** A lost fill is a position the system
    does not know it holds. There is no acceptable amount of that.
  * **RPO for market history is one bar.** It is re-fetchable from the venue,
    so losing some costs a backfill, not correctness.
  * **RTO is bounded by the position risk, not by convenience.** With open
    positions and no running bot, nothing is managing the stops -- so the
    objective is tight. With a flat book it can be relaxed, and saying so
    prevents the tight number from being quietly ignored as unrealistic.

The drill (`run_restore_drill`) is deliberately cheap enough to run on a
schedule. A drill that needs a maintenance window is a drill that gets
skipped, and a backup nobody has restored is not a backup.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Final


class DataClass(Enum):
    """What is being recovered. Different data, different objectives."""

    TRADE_RECORDS = "trade_records"
    POSITION_STATE = "position_state"
    AUDIT_TRAIL = "audit_trail"
    MARKET_HISTORY = "market_history"
    MODEL_ARTIFACTS = "model_artifacts"
    CONFIGURATION = "configuration"


class BookState(Enum):
    """Whether the bot is holding risk while it is down."""

    OPEN_POSITIONS = "open_positions"
    FLAT = "flat"


@dataclass(frozen=True)
class Objective:
    data_class: DataClass
    rpo_seconds: float  # acceptable data loss
    rationale: str

    def tolerates_loss(self) -> bool:
        return self.rpo_seconds > 0.0


# Recovery *point* objectives -- how much data may be lost.
_RPO: Final[dict[DataClass, Objective]] = {
    DataClass.TRADE_RECORDS: Objective(
        DataClass.TRADE_RECORDS,
        0.0,
        "A lost fill is a position the system does not know it holds. The "
        "reconciliation pass can find it at the venue, but only if the venue "
        "is reachable -- and the case where it is not is exactly a disaster.",
    ),
    DataClass.POSITION_STATE: Objective(
        DataClass.POSITION_STATE,
        0.0,
        "Same reason: reconstructable from trade records, and worthless if "
        "those are gone too.",
    ),
    DataClass.AUDIT_TRAIL: Objective(
        DataClass.AUDIT_TRAIL,
        0.0,
        "The audit trail's only job is to survive the incident that makes "
        "somebody read it.",
    ),
    DataClass.MARKET_HISTORY: Objective(
        DataClass.MARKET_HISTORY,
        60.0,
        "Re-fetchable from the venue. Losing a minute costs a backfill, not "
        "correctness.",
    ),
    DataClass.MODEL_ARTIFACTS: Objective(
        DataClass.MODEL_ARTIFACTS,
        86_400.0,
        "Reproducible from data and a training run. A day's loss costs compute.",
    ),
    DataClass.CONFIGURATION: Objective(
        DataClass.CONFIGURATION,
        0.0,
        "In version control, so the objective is trivially met -- stated anyway "
        "so that config drifting out of the repository is visible as a gap.",
    ),
}

# Recovery *time* objectives -- how long the system may stay down. Keyed on
# whether risk is open, because the honest number differs by an order of
# magnitude and a single number would be wrong in both directions.
_RTO: Final[dict[BookState, float]] = {
    # Nothing is managing the stops while the bot is down. Fifteen minutes is
    # the point at which an unmanaged position stops being an inconvenience.
    BookState.OPEN_POSITIONS: 900.0,
    # Flat, the cost of being down is opportunity, so the bound is a working
    # day rather than a fire drill.
    BookState.FLAT: 28_800.0,
}


@dataclass(frozen=True)
class DrillResult:
    """One restore drill. Numbers, not adjectives."""

    data_class: DataClass
    restored: bool
    elapsed_s: float
    data_age_s: float
    book_state: BookState
    detail: str = ""

    @property
    def met_rpo(self) -> bool:
        return self.restored and self.data_age_s <= rpo_for(self.data_class)

    @property
    def met_rto(self) -> bool:
        return self.restored and self.elapsed_s <= rto_for(self.book_state)

    @property
    def passed(self) -> bool:
        return self.met_rpo and self.met_rto


def rpo_for(data_class: DataClass) -> float:
    """Acceptable data loss, in seconds. Undeclared classes tolerate none."""
    objective = _RPO.get(data_class)
    return objective.rpo_seconds if objective else 0.0


def rto_for(book_state: BookState) -> float:
    """Acceptable downtime, in seconds. Undeclared states get the tight bound."""
    return _RTO.get(book_state, _RTO[BookState.OPEN_POSITIONS])


def objective_for(data_class: DataClass) -> Objective:
    declared = _RPO.get(data_class)
    if declared is not None:
        return declared
    # An undeclared class gets the strictest objective rather than a lenient
    # default: nobody has decided how much of it may be lost, so the answer is
    # none until somebody does.
    return Objective(
        data_class,
        0.0,
        "No objective is declared for this data class; it therefore tolerates "
        "no loss until somebody states otherwise.",
    )


def run_restore_drill(
    data_class: DataClass,
    restore: Callable[[], float],
    book_state: BookState = BookState.OPEN_POSITIONS,
    clock: Callable[[], float] = time.monotonic,
) -> DrillResult:
    """
    Run one restore and measure it against the objectives.

    *restore* performs the restore and returns the age, in seconds, of the
    newest datum it recovered -- which is the measured RPO. It is a callable
    rather than a fixed implementation because the drill has to work against
    a real backup in production and a fixture in a test, and a drill that only
    runs in one of those places is the one that goes stale.

    An exception from *restore* is a failed drill, not a crashed drill run: a
    restore that throws is exactly the outcome this is looking for.
    """
    started = clock()
    try:
        data_age_s = float(restore())
        restored = True
        detail = ""
    except Exception as exc:  # noqa: BLE001 - a failed restore is the finding
        data_age_s = float("inf")
        restored = False
        detail = f"{type(exc).__name__}: {exc}"
    elapsed = clock() - started

    return DrillResult(
        data_class=data_class,
        restored=restored,
        elapsed_s=elapsed,
        data_age_s=data_age_s,
        book_state=book_state,
        detail=detail,
    )


def declared_data_classes() -> frozenset[DataClass]:
    return frozenset(_RPO)


def objectives_table() -> tuple[Objective, ...]:
    return tuple(_RPO[c] for c in DataClass if c in _RPO)
