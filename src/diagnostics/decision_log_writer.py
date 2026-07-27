"""
Self-updating decision log — v10 Fully Autonomous Multi-Decade Operation.

Every structural change to strategy mix, risk limits, or venues should be
reconstructable by a human auditor without tribal knowledge. This module
formats a structural-change record into a DECISION_LOG.md-compatible
Markdown entry and appends it — pure string formatting + file append, no
interpretation of *why* a change is good or bad (that judgment stays with
whatever system emits the record, e.g. v4's model promotion or v6's
strategy gauntlet).

Authority:
  - Domain Prior: no hidden failures or skipped validation — the
    justification for every automated structural change must be captured
    at the moment it happens, not reconstructed later from logs
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StructuralChangeRecord:
    """One structural change to log: what changed, why, and the evidence."""

    title: str
    change_type: str  # e.g. "strategy_promoted", "model_promoted", "strategy_retired"
    justification: str
    evidence: dict[str, str | float | int]


def format_decision_log_entry(record: StructuralChangeRecord, at: datetime | None = None) -> str:
    """Pure formatting — returns the Markdown block, does not write anything."""
    timestamp = (at or datetime.now(tz=UTC)).strftime("%Y-%m-%d")
    evidence_lines = "\n".join(f"- **{k}**: {v}" for k, v in sorted(record.evidence.items()))
    return (
        f"## {timestamp} — {record.title} ({record.change_type})\n\n"
        f"{record.justification}\n\n"
        f"**Evidence**:\n{evidence_lines}\n"
    )


def append_to_decision_log(
    record: StructuralChangeRecord, log_path: Path, at: datetime | None = None
) -> None:
    """
    Appends the formatted entry to the given DECISION_LOG.md-style file.
    Creates the file with a header if it does not yet exist. Never
    truncates or rewrites existing content — append-only, matching the
    audit trail's immutability discipline (v8).
    """
    entry = format_decision_log_entry(record, at)
    if not log_path.exists():
        log_path.write_text("# Decision Log\n\n", encoding="utf-8")
    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n" + entry)
