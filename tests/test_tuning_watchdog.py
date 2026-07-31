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


def test_probation_status_locked_while_locked(tmp_path: Path) -> None:
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

    assert watchdog.probation_status("hmm.entropy_threshold") == WatchdogOutcome.LOCKED


def test_is_locked_expires_after_cooldown(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

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

    assert watchdog.is_locked("hmm.entropy_threshold")
    # Force the lock to expire by backdating the stored timestamp
    with watchdog._lock:
        watchdog._locked_until["hmm.entropy_threshold"] = datetime.now(UTC) - timedelta(hours=1)

    assert not watchdog.is_locked("hmm.entropy_threshold")


def test_multiple_params_tracked_independently(tmp_path: Path) -> None:
    watchdog, store, _ = build(tmp_path, probation_trades=5)
    store.promote("param_a", 0.1, {})
    store.promote("param_b", 0.2, {})
    watchdog.start_probation("param_a", make_baseline())
    watchdog.start_probation("param_b", make_baseline())

    # param_a not in probation result yet
    outcome_a = watchdog.record_trade_outcome(
        "param_a",
        pnl_usd=5.0,
        predicted_prob=0.6,
        actual_direction=1,
        current_equity=1005.0,
        starting_equity=1000.0,
    )
    assert outcome_a == WatchdogOutcome.IN_PROBATION

    # param_b still independent
    assert watchdog.probation_status("param_b") == WatchdogOutcome.IN_PROBATION


def test_start_probation_replaces_existing(tmp_path: Path) -> None:
    watchdog, store, _ = build(tmp_path, probation_trades=10)
    store.promote("param_a", 0.1, {})
    baseline1 = make_baseline()
    watchdog.start_probation("param_a", baseline1)

    # Replace with new baseline
    baseline2 = make_baseline()
    watchdog.start_probation("param_a", baseline2)

    # Should still be in probation (not rolled back or cleared)
    assert watchdog.probation_status("param_a") == WatchdogOutcome.IN_PROBATION
