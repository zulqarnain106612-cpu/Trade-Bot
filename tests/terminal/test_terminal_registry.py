"""
TERM-002 -- the process registry reports what was observed, nothing more.

The process center is only as honest as this registry. An entry is running
until something proves it finished; a finished entry leaves the active list
exactly once and keeps its exit status, failure reason and the tail of its
output; a command whose status nobody could see is ``unknown``, never
``succeeded``. History and retained output are bounded so a long-running
daemon cannot grow without limit.

Decides:
  - TERM-002 -- process identity and exit status are observed, not invented
"""

from __future__ import annotations

import base64

import pytest

from src.terminal.protocol import ProtocolError
from src.terminal.registry import ProcessRegistry, outcome


@pytest.fixture
def clock():
    state = {"wall": 1000.0, "mono": 50.0}
    return state


@pytest.fixture
def events():
    return []


@pytest.fixture
def registry(clock, events):
    return ProcessRegistry(
        history_limit=3,
        output_limit=8,
        on_event=lambda kind, payload: events.append((kind, payload)),
        clock=lambda: clock["wall"],
        monotonic=lambda: clock["mono"],
    )


class TestOutcome:
    @pytest.mark.parametrize(
        ("exit_code", "signal_name", "reported", "expected"),
        [
            (0, None, None, ("succeeded", None)),
            (2, None, None, ("failed", "exited with status 2")),
            (None, "SIGKILL", None, ("failed", "terminated by SIGKILL")),
            (None, None, None, ("unknown", None)),
            (None, None, True, ("succeeded", None)),
            (None, None, False, ("failed", "reported failure")),
            # Observed evidence wins over an application's own report.
            (3, None, True, ("failed", "exited with status 3")),
        ],
    )
    def test_state_follows_the_evidence(self, exit_code, signal_name, reported, expected):
        assert outcome(exit_code, signal_name, reported) == expected


class TestLifecycle:
    def test_begin_reports_a_running_entry(self, registry, events):
        entry = registry.begin(
            kind="command", title="pytest -q", session_id="s-00000001", pid=42, pgid=42
        )
        assert entry.state == "running"
        assert events[-1][0] == "process"
        assert events[-1][1]["pid"] == 42
        assert registry.active() == [entry]
        assert registry.active_for_session("s-00000001") == [entry]
        assert registry.active_for_session("s-00000002") == []

    def test_an_unknown_kind_is_a_programming_error(self, registry):
        with pytest.raises(ValueError):
            registry.begin(kind="daemon", title="x")

    def test_a_client_chosen_id_cannot_collide(self, registry):
        registry.begin(kind="app", title="retrain", entry_id="p-0000000a")
        with pytest.raises(ProtocolError) as exc_info:
            registry.begin(kind="app", title="again", entry_id="p-0000000a")
        assert exc_info.value.code == "duplicate_id"
        registry.finish("p-0000000a", exit_code=0)
        with pytest.raises(ProtocolError):
            registry.begin(kind="app", title="after", entry_id="p-0000000a")

    def test_update_emits_only_on_change(self, registry, events):
        entry = registry.begin(kind="command", title="t")
        count = len(events)
        registry.update(entry.id, title="t")
        assert len(events) == count
        registry.update(entry.id, cpu_percent=12.5)
        assert len(events) == count + 1
        with pytest.raises(AttributeError):
            registry.update(entry.id, not_a_field=1)

    def test_finishing_moves_the_entry_to_history_exactly_once(self, registry, events, clock):
        entry = registry.begin(kind="job", title="make", pid=7, pgid=7)
        clock["wall"] = 1010.0
        registry.finish(entry.id, exit_code=1, output=b"boom")
        assert registry.active() == []
        snapshot = registry.snapshot()
        assert snapshot["active"] == []
        (done,) = snapshot["history"]
        assert done["state"] == "failed"
        assert done["exit_code"] == 1
        assert done["failure_reason"] == "exited with status 1"
        assert done["ended_at"] == 1010.0
        assert done["elapsed_s"] is None
        assert events[-1][0] == "process_done"
        with pytest.raises(ProtocolError) as exc_info:
            registry.finish(entry.id, exit_code=0)
        assert exc_info.value.code == "not_running"
        assert registry.get(entry.id).exit_code == 1

    def test_an_explicit_reason_is_kept(self, registry):
        entry = registry.begin(kind="command", title="x")
        registry.finish(entry.id, exit_code=None, reason="the shell exited first")
        assert registry.get(entry.id).failure_reason == "the shell exited first"
        assert registry.get(entry.id).state == "unknown"

    def test_operations_on_unknown_or_finished_entries_are_refused(self, registry):
        with pytest.raises(ProtocolError):
            registry.get("p-ffffffff")
        with pytest.raises(ProtocolError):
            registry.update("p-ffffffff", title="x")
        with pytest.raises(ProtocolError):
            registry.append_output("p-ffffffff", b"x")


class TestBounds:
    def test_history_is_bounded_and_newest_first(self, registry):
        for n in range(5):
            entry = registry.begin(kind="command", title=f"cmd {n}")
            registry.finish(entry.id, exit_code=0)
        titles = [e["title"] for e in registry.snapshot()["history"]]
        assert titles == ["cmd 4", "cmd 3", "cmd 2"]

    def test_finished_output_keeps_the_tail(self, registry):
        entry = registry.begin(kind="job", title="noisy")
        registry.finish(entry.id, exit_code=0, output=b"0123456789ABCDEF")
        detail = registry.get(entry.id).to_dict(include_output=True)
        assert base64.b64decode(detail["output"]) == b"89ABCDEF"
        assert detail["output_truncated"] is True

    def test_app_job_output_accumulates_and_keeps_the_tail(self, registry):
        entry = registry.begin(kind="app", title="backfill")
        registry.append_output(entry.id, b"12345")
        assert registry.get(entry.id).output_truncated is False
        registry.append_output(entry.id, b"67890")
        assert registry.get(entry.id).output == b"34567890"
        assert registry.get(entry.id).output_truncated is True

    def test_running_entries_report_elapsed_time_from_the_monotonic_clock(self, registry, clock):
        registry.begin(kind="command", title="sleep")
        clock["mono"] = 62.5
        (active,) = registry.snapshot()["active"]
        assert active["elapsed_s"] == 12.5

    def test_a_registry_without_a_listener_still_works(self):
        quiet = ProcessRegistry(history_limit=1, output_limit=1)
        entry = quiet.begin(kind="command", title="x")
        quiet.finish(entry.id, exit_code=0)
        assert quiet.get(entry.id).state == "succeeded"
