"""Tests for the v10 self-updating decision log writer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from src.diagnostics.decision_log_writer import (
    StructuralChangeRecord,
    append_to_decision_log,
    format_decision_log_entry,
)


def _record() -> StructuralChangeRecord:
    return StructuralChangeRecord(
        title="mean_reversion_pairs_v1 promoted",
        change_type="strategy_promoted",
        justification="Cleared the v6 promotion gauntlet after 45 days paper trading.",
        evidence={"sharpe": 1.4, "trade_count": 62, "max_drawdown_pct": 0.08},
    )


def test_format_includes_title_and_type() -> None:
    entry = format_decision_log_entry(_record(), at=datetime(2026, 7, 27, tzinfo=UTC))
    assert "2026-07-27" in entry
    assert "mean_reversion_pairs_v1 promoted" in entry
    assert "strategy_promoted" in entry


def test_format_includes_all_evidence_keys() -> None:
    entry = format_decision_log_entry(_record())
    assert "sharpe" in entry
    assert "trade_count" in entry
    assert "max_drawdown_pct" in entry


def test_append_creates_file_with_header_if_missing(tmp_path: Path) -> None:
    log_path = tmp_path / "DECISION_LOG.md"
    append_to_decision_log(_record(), log_path)
    content = log_path.read_text(encoding="utf-8")
    assert content.startswith("# Decision Log")
    assert "mean_reversion_pairs_v1 promoted" in content


def test_append_does_not_truncate_existing_content(tmp_path: Path) -> None:
    log_path = tmp_path / "DECISION_LOG.md"
    log_path.write_text("# Decision Log\n\nExisting entry.\n", encoding="utf-8")
    append_to_decision_log(_record(), log_path)
    content = log_path.read_text(encoding="utf-8")
    assert "Existing entry." in content
    assert "mean_reversion_pairs_v1 promoted" in content


def test_append_twice_keeps_both_entries(tmp_path: Path) -> None:
    log_path = tmp_path / "DECISION_LOG.md"
    append_to_decision_log(_record(), log_path)
    second = StructuralChangeRecord(
        title="funding_carry_v1 retired",
        change_type="strategy_retired",
        justification="CUSUM decay detector confirmed structural edge loss.",
        evidence={"cusum_statistic": 6.2},
    )
    append_to_decision_log(second, log_path)
    content = log_path.read_text(encoding="utf-8")
    assert "mean_reversion_pairs_v1 promoted" in content
    assert "funding_carry_v1 retired" in content
