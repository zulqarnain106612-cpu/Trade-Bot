"""
RES-003, RES-004 — objectives with numbers, and a drill that can fail.

"RTO: minutes. RPO: near-zero." Both are unfalsifiable, which is why every
disaster-recovery document contains them and why none of them has ever caught
anything. The tests below require an actual number for every data class, and
require the drill to fail when a restore is slow, stale, or throws.

The asymmetry between data classes is the interesting part and is asserted
directly: trade records tolerate no loss because a lost fill is a position the
system does not know it holds, while market history tolerates a minute because
it is re-fetchable from the venue. A single RPO across both would be either
impossible or meaningless.
"""

from __future__ import annotations

import pytest

from src.diagnostics.recovery_objectives import (
    BookState,
    DataClass,
    DrillResult,
    declared_data_classes,
    objective_for,
    objectives_table,
    rpo_for,
    rto_for,
    run_restore_drill,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class TestEveryObjectiveIsANumber:
    @pytest.mark.parametrize("data_class", list(DataClass))
    def test_every_data_class_has_a_declared_objective(self, data_class):
        assert data_class in declared_data_classes()

    @pytest.mark.parametrize("data_class", list(DataClass))
    def test_the_objective_is_finite_and_non_negative(self, data_class):
        value = rpo_for(data_class)
        assert value >= 0.0
        assert value != float("inf")

    @pytest.mark.parametrize("data_class", list(DataClass))
    def test_every_objective_is_justified(self, data_class):
        # A number with no reasoning cannot be revised sensibly when it turns
        # out to be wrong.
        assert len(objective_for(data_class).rationale) > 40

    def test_the_table_covers_the_enum_exactly(self):
        assert {o.data_class for o in objectives_table()} == set(DataClass)


class TestTheObjectivesSayWhatTheyShould:
    @pytest.mark.parametrize(
        "data_class",
        [DataClass.TRADE_RECORDS, DataClass.POSITION_STATE, DataClass.AUDIT_TRAIL],
    )
    def test_money_and_evidence_tolerate_no_loss(self, data_class):
        assert rpo_for(data_class) == 0.0
        assert objective_for(data_class).tolerates_loss() is False

    def test_market_history_tolerates_some_loss(self):
        # Re-fetchable from the venue: losing a minute costs a backfill.
        assert rpo_for(DataClass.MARKET_HISTORY) > 0.0

    def test_model_artifacts_tolerate_the_most(self):
        assert rpo_for(DataClass.MODEL_ARTIFACTS) > rpo_for(DataClass.MARKET_HISTORY)

    def test_an_undeclared_class_tolerates_nothing(self):
        from src.diagnostics import recovery_objectives

        saved = recovery_objectives._RPO.pop(DataClass.MARKET_HISTORY)
        try:
            assert rpo_for(DataClass.MARKET_HISTORY) == 0.0
            assert "No objective is declared" in objective_for(DataClass.MARKET_HISTORY).rationale
        finally:
            recovery_objectives._RPO[DataClass.MARKET_HISTORY] = saved


class TestTheRecoveryTimeObjective:
    def test_open_positions_get_the_tight_bound(self):
        # Nothing is managing the stops while the bot is down.
        assert rto_for(BookState.OPEN_POSITIONS) < rto_for(BookState.FLAT)

    def test_the_tight_bound_is_a_real_number_of_minutes(self):
        assert 0 < rto_for(BookState.OPEN_POSITIONS) <= 1800

    def test_an_unknown_book_state_gets_the_tight_bound(self):
        from src.diagnostics import recovery_objectives

        saved = recovery_objectives._RTO.pop(BookState.FLAT)
        try:
            assert rto_for(BookState.FLAT) == rto_for(BookState.OPEN_POSITIONS)
        finally:
            recovery_objectives._RTO[BookState.FLAT] = saved


class TestTheDrill:
    def test_a_fast_fresh_restore_passes(self):
        clock = FakeClock()

        def restore() -> float:
            clock.t += 30.0  # took 30 seconds
            return 0.0  # nothing lost

        result = run_restore_drill(DataClass.TRADE_RECORDS, restore, clock=clock)
        assert result.passed
        assert result.met_rpo and result.met_rto

    def test_a_slow_restore_fails_the_time_objective(self):
        clock = FakeClock()

        def restore() -> float:
            clock.t += rto_for(BookState.OPEN_POSITIONS) + 1
            return 0.0

        result = run_restore_drill(DataClass.TRADE_RECORDS, restore, clock=clock)
        assert result.met_rpo is True
        assert result.met_rto is False
        assert result.passed is False

    def test_a_stale_restore_fails_the_point_objective(self):
        clock = FakeClock()

        def restore() -> float:
            clock.t += 10.0
            return 120.0  # two minutes of trades missing

        result = run_restore_drill(DataClass.TRADE_RECORDS, restore, clock=clock)
        assert result.met_rto is True
        assert result.met_rpo is False

    def test_the_same_staleness_passes_for_market_history(self):
        # The asymmetry, exercised rather than asserted about the constants.
        clock = FakeClock()

        def restore() -> float:
            clock.t += 10.0
            return 30.0

        assert run_restore_drill(DataClass.MARKET_HISTORY, restore, clock=clock).passed

    def test_a_restore_that_throws_is_a_failed_drill_not_a_crash(self):
        # A restore that raises is exactly the outcome a drill is looking for;
        # propagating the exception would make the drill run look broken
        # instead of the backup.
        def restore() -> float:
            raise FileNotFoundError("backup-2026-09-11.tar.gz.enc")

        result = run_restore_drill(DataClass.TRADE_RECORDS, restore, clock=FakeClock())
        assert result.restored is False
        assert result.passed is False
        assert "FileNotFoundError" in result.detail

    def test_a_failed_restore_meets_neither_objective(self):
        def restore() -> float:
            raise RuntimeError("decryption failed")

        result = run_restore_drill(DataClass.AUDIT_TRAIL, restore, clock=FakeClock())
        assert result.met_rpo is False
        assert result.met_rto is False

    def test_a_flat_book_relaxes_the_time_objective(self):
        clock = FakeClock()

        def restore() -> float:
            clock.t += rto_for(BookState.OPEN_POSITIONS) + 60
            return 0.0

        result = run_restore_drill(
            DataClass.TRADE_RECORDS, restore, book_state=BookState.FLAT, clock=clock
        )
        assert result.passed


class TestTheResultIsSelfDescribing:
    def test_it_carries_the_measurements_not_just_a_verdict(self):
        # A drill that reports "pass" and nothing else cannot show a trend,
        # and the trend is how you see a restore getting slower each month.
        result = DrillResult(
            data_class=DataClass.TRADE_RECORDS,
            restored=True,
            elapsed_s=42.0,
            data_age_s=0.0,
            book_state=BookState.OPEN_POSITIONS,
        )
        assert result.elapsed_s == 42.0
        assert result.data_age_s == 0.0
        assert result.book_state is BookState.OPEN_POSITIONS
