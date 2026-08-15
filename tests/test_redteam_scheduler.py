"""Tests for the v10 periodic red-team scheduler."""

from __future__ import annotations

import pytest

from src.tuning.redteam_scheduler import RedTeamScheduler


def test_rejects_nonpositive_interval() -> None:
    with pytest.raises(ValueError, match="interval_ms"):
        RedTeamScheduler(interval_ms=0)


def test_never_run_is_immediately_due() -> None:
    scheduler = RedTeamScheduler(interval_ms=1000)
    assert scheduler.is_due(now_ms=0)
    assert scheduler.ms_until_due(now_ms=0) == 0


def test_not_due_immediately_after_run() -> None:
    scheduler = RedTeamScheduler(interval_ms=1000)
    scheduler.record_run(ran_at_ms=0, breached_floor=False)
    assert not scheduler.is_due(now_ms=500)


def test_due_after_interval_elapses() -> None:
    scheduler = RedTeamScheduler(interval_ms=1000)
    scheduler.record_run(ran_at_ms=0, breached_floor=False)
    assert scheduler.is_due(now_ms=1000)
    assert scheduler.is_due(now_ms=1500)


def test_ms_until_due_counts_down() -> None:
    scheduler = RedTeamScheduler(interval_ms=1000)
    scheduler.record_run(ran_at_ms=0, breached_floor=False)
    assert scheduler.ms_until_due(now_ms=300) == 700


def test_last_run_records_breach_flag() -> None:
    scheduler = RedTeamScheduler(interval_ms=1000)
    scheduler.record_run(ran_at_ms=100, breached_floor=True)
    assert scheduler.last_run is not None
    assert scheduler.last_run.breached_floor
    assert scheduler.last_run.ran_at_ms == 100


def test_last_run_none_initially() -> None:
    scheduler = RedTeamScheduler()
    assert scheduler.last_run is None
