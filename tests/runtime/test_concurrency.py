"""RES-019: concurrent requests cannot interleave on one component -- one open
change wins, an approved change executes exactly once, and no thread sees a
half-applied transition.

Decides: RES-019"""

from __future__ import annotations

import threading
from collections import Counter

from src.runtime.changes import ChangeError, ChangeRequest, ChangeStatus
from src.runtime.contracts import LifecycleAction, LifecycleState

from ._support import HUMAN, build_platform, spec

A = LifecycleAction
S = LifecycleState
N = 8


def _race(target, n: int = N) -> None:  # type: ignore[no-untyped-def]
    barrier = threading.Barrier(n)

    def run(i: int) -> None:
        barrier.wait()
        target(i)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_concurrent_requests_leave_exactly_one_open_change() -> None:
    p = build_platform()
    p.registry.register(spec("w"), observed_state=S.STOPPED)
    statuses: list[ChangeStatus] = []
    lock = threading.Lock()

    def submit(i: int) -> None:
        change = p.changes.submit(ChangeRequest("worker:w", A.START, HUMAN, f"start {i}"))
        with lock:
            statuses.append(change.status)

    _race(submit)
    assert Counter(statuses) == {ChangeStatus.AWAITING_APPROVAL: 1, ChangeStatus.REJECTED: N - 1}


def test_an_approved_change_executes_exactly_once() -> None:
    p = build_platform()
    p.registry.register(spec("w"), observed_state=S.STOPPED)
    change = p.changes.submit(ChangeRequest("worker:w", A.START, HUMAN, "start"))
    p.changes.approve(change.change_id, HUMAN)
    outcomes: list[str] = []
    lock = threading.Lock()

    def execute(i: int) -> None:
        try:
            result = p.changes.execute(change.change_id, HUMAN).status.value
        except ChangeError:
            result = "refused"
        with lock:
            outcomes.append(result)

    _race(execute)
    assert Counter(outcomes) == {"EXECUTED": 1, "refused": N - 1}
    assert [a for _, a, _ in p.controller.calls] == [A.START]
    assert p.registry.get("worker:w").state is S.STANDBY
