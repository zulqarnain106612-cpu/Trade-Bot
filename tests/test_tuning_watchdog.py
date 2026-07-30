from pathlib import Path

from src.config import SelfTuningSettings
from src.risk.performance_drift import PerformanceBaseline
from src.tuning.audit import TuningAuditLog, TuningEventType
from src.tuning.store import VersionedConfigStore
from src.tuning.watchdog import PostPromotionWatchdog, WatchdogOutcome


def make_baseline() -> PerformanceBaseline:
    return PerformanceBaseline(
        train_sharpe=2.0,
        oos_sharpe=1.8,
        train_accuracy=0.60,
        oos_accuracy=0.58,
        train_win_rate=0.55,
        max_drawdown_pct=0.05,
        trades_in_backtest=1000,
    )


def build(
    tmp_path: Path, probation_trades: int = 50
) -> tuple[PostPromotionWatchdog, VersionedConfigStore, TuningAuditLog]:
    store = VersionedConfigStore(tmp_path / "versions.jsonl")
    audit = TuningAuditLog(tmp_path / "audit.jsonl")
    settings = SelfTuningSettings(probation_trades=probation_trades, probation_hours=999.0)
    watchdog = PostPromotionWatchdog(store, audit, settings)
    return watchdog, store, audit


def test_record_trade_when_not_in_probation_is_noop(tmp_path: Path) -> None:
    watchdog, _, _ = build(tmp_path)
    outcome = watchdog.record_trade_outcome(
        "hmm.entropy_threshold",
        pnl_usd=10.0,
        predicted_prob=0.6,
        actual_direction=1,
        current_equity=1010.0,
        starting_equity=1000.0,
    )
    assert outcome == WatchdogOutcome.NOT_IN_PROBATION


def test_healthy_trades_stay_in_probation_until_cleared(tmp_path: Path) -> None:
    watchdog, store, _ = build(tmp_path, probation_trades=5)
    store.promote("hmm.entropy_threshold", 0.55, {})
    watchdog.start_probation("hmm.entropy_threshold", make_baseline())

    for i in range(4):
        outcome = watchdog.record_trade_outcome(
            "hmm.entropy_threshold",
            pnl_usd=5.0,
            predicted_prob=0.6,
            actual_direction=1,
            current_equity=1000.0 + i,
            starting_equity=1000.0,
        )
        assert outcome == WatchdogOutcome.IN_PROBATION

    final_outcome = watchdog.record_trade_outcome(
        "hmm.entropy_threshold",
        pnl_usd=5.0,
        predicted_prob=0.6,
        actual_direction=1,
        current_equity=1010.0,
        starting_equity=1000.0,
    )
    assert final_outcome == WatchdogOutcome.CLEARED
    assert watchdog.probation_status("hmm.entropy_threshold") == WatchdogOutcome.NOT_IN_PROBATION


def test_drift_triggers_rollback_and_lock(tmp_path: Path) -> None:
    watchdog, store, audit = build(tmp_path, probation_trades=100)
    store.promote("hmm.entropy_threshold", 0.50, {"note": "champion"})
    store.promote("hmm.entropy_threshold", 0.65, {"note": "promoted challenger"})
    watchdog.start_probation("hmm.entropy_threshold", make_baseline())

    outcome = WatchdogOutcome.IN_PROBATION
    for i in range(35):
        outcome = watchdog.record_trade_outcome(
            "hmm.entropy_threshold",
            pnl_usd=-50.0,
            predicted_prob=0.6,
            actual_direction=1,
            current_equity=1000.0 - i * 50,
            starting_equity=1000.0,
        )
        if outcome == WatchdogOutcome.ROLLED_BACK:
            break

    assert outcome == WatchdogOutcome.ROLLED_BACK
    assert store.current("hmm.entropy_threshold").value == 0.50
    assert store.current("hmm.entropy_threshold").is_rollback is True
    assert watchdog.is_locked("hmm.entropy_threshold")
    events = [e.event_type for e in audit.read_for_param("hmm.entropy_threshold")]
    assert TuningEventType.ROLLED_BACK in events


def test_probation_status_not_in_probation_by_default(tmp_path: Path) -> None:
    watchdog, _, _ = build(tmp_path)
    assert watchdog.probation_status("hmm.entropy_threshold") == WatchdogOutcome.NOT_IN_PROBATION


def test_is_locked_false_when_never_locked(tmp_path: Path) -> None:
    watchdog, _, _ = build(tmp_path)
    assert not watchdog.is_locked("hmm.entropy_threshold")


def test_probation_status_returns_locked_after_rollback(tmp_path: Path) -> None:
    watchdog, store, _ = build(tmp_path, probation_trades=100)
    store.promote("hmm.entropy_threshold", 0.50, {})
    store.promote("hmm.entropy_threshold", 0.65, {})
    watchdog.start_probation("hmm.entropy_threshold", make_baseline())

    for i in range(35):
        outcome = watchdog.record_trade_outcome(
            "hmm.entropy_threshold",
            pnl_usd=-50.0,
            predicted_prob=0.6,
            actual_direction=1,
            current_equity=1000.0 - i * 50,
            starting_equity=1000.0,
        )
        if outcome == WatchdogOutcome.ROLLED_BACK:
            break

    # After rollback the param should be locked (not just NOT_IN_PROBATION)
    status = watchdog.probation_status("hmm.entropy_threshold")
    assert status == WatchdogOutcome.LOCKED


def test_lock_expires_after_cooldown(tmp_path: Path) -> None:
    store = VersionedConfigStore(tmp_path / "versions.jsonl")
    audit = TuningAuditLog(tmp_path / "audit.jsonl")
    # Use a very short cooldown (0 hours) so the lock is already expired
    settings = SelfTuningSettings(
        probation_trades=100, probation_hours=999.0, min_hours_between_attempts=0.0
    )
    watchdog = PostPromotionWatchdog(store, audit, settings)

    store.promote("hmm.entropy_threshold", 0.50, {})
    store.promote("hmm.entropy_threshold", 0.65, {})
    watchdog.start_probation("hmm.entropy_threshold", make_baseline())

    # Force a rollback
    for i in range(35):
        outcome = watchdog.record_trade_outcome(
            "hmm.entropy_threshold",
            pnl_usd=-50.0,
            predicted_prob=0.6,
            actual_direction=1,
            current_equity=1000.0 - i * 50,
            starting_equity=1000.0,
        )
        if outcome == WatchdogOutcome.ROLLED_BACK:
            break

    # With 0-hour cooldown the lock should already be expired on the next check
    assert not watchdog.is_locked("hmm.entropy_threshold")


def test_start_probation_while_already_in_probation_overwrites(tmp_path: Path) -> None:
    watchdog, store, _ = build(tmp_path, probation_trades=100)
    store.promote("hmm.entropy_threshold", 0.55, {})
    watchdog.start_probation("hmm.entropy_threshold", make_baseline())
    # Restart probation — should not raise, should reset state
    watchdog.start_probation("hmm.entropy_threshold", make_baseline())
    assert watchdog.probation_status("hmm.entropy_threshold") == WatchdogOutcome.IN_PROBATION
