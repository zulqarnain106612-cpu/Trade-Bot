"""
A rollback the store cannot satisfy must still end probation.

The watchdog called store.rollback() bare inside record_trade_outcome. When
the store has no safe earlier value to revert to it raises, and the
exception escaped before the audit record, the probation deletion and the
lockout ran -- so the parameter stayed in probation and every subsequent
closed trade re-detected the same drift and raised again.
"""

from __future__ import annotations

import time
import types

from src.tuning.audit import TuningEventType
from src.tuning.store import NoPriorVersionError
from src.tuning.watchdog import PostPromotionWatchdog, WatchdogOutcome, _ProbationState


class _RaisingStore:
    def rollback(self, param_name: str):
        raise NoPriorVersionError(param_name)


class _OkStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def rollback(self, param_name: str):
        self.calls.append(param_name)
        return None


class _RecordingLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, TuningEventType, dict]] = []

    def record(self, param_name, event_type, payload):
        self.events.append((param_name, event_type, payload))


class _AlwaysDrifts:
    def record_trade_outcome(self, *_a, **_k) -> None:
        return None

    def check_drift(self):
        return types.SimpleNamespace(
            drifted=True,
            reason="sharpe_collapse",
            metric="sharpe",
            live_value=-0.4,
            baseline_value=1.1,
        )


def _watchdog(store):
    settings = types.SimpleNamespace(
        min_hours_between_attempts=6,
        probation_trades=20,
        probation_hours=48,
    )
    log = _RecordingLog()
    wd = PostPromotionWatchdog(store, log, settings)
    wd._probations["p"] = _ProbationState(detector=_AlwaysDrifts(), started_at=time.monotonic())
    return wd, log


def _feed(wd):
    return wd.record_trade_outcome("p", -50.0, 0.6, -1, 9_500.0, 10_000.0)


def test_exhausted_history_does_not_raise_out_of_the_watchdog() -> None:
    wd, _log = _watchdog(_RaisingStore())
    assert _feed(wd) is WatchdogOutcome.ROLLED_BACK


def test_probation_ends_even_when_the_store_cannot_roll_back() -> None:
    wd, _log = _watchdog(_RaisingStore())
    _feed(wd)

    assert wd.is_locked("p") is True
    # Second trade must not re-enter the drift branch.
    assert _feed(wd) is WatchdogOutcome.NOT_IN_PROBATION


def test_a_failed_rollback_is_audited_as_paused_not_rolled_back() -> None:
    wd, log = _watchdog(_RaisingStore())
    _feed(wd)

    param, event, payload = log.events[-1]
    assert (param, event) == ("p", TuningEventType.PAUSED)
    assert payload["rolled_back"] is False
    assert payload["reason"] == "sharpe_collapse"


def test_a_successful_rollback_is_still_audited_as_rolled_back() -> None:
    store = _OkStore()
    wd, log = _watchdog(store)
    assert _feed(wd) is WatchdogOutcome.ROLLED_BACK

    _param, event, payload = log.events[-1]
    assert event is TuningEventType.ROLLED_BACK
    assert payload["rolled_back"] is True
    assert store.calls == ["p"]
    assert wd.is_locked("p") is True
