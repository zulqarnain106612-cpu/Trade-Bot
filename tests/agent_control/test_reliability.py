"""GOV-057 / GOV-063 -- deterministic agent reliability controls."""

from __future__ import annotations

import json

from src.agent_control import reliability


def test_repeated_tool_failure_is_escalated(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    for _ in range(3):
        event = reliability.record_tool_failure(
            tool="Bash", command="pytest -q", summary="FAILED tests/x.py::test_x"
        )
    assert event["repeat_count"] == 3
    assert event["status"] == "repeat"
    assert json.loads(reliability.state_path().read_text())["events"][-1]["status"] == "repeat"


def test_ci_verdict_is_bound_to_exact_head(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    reliability.record_ci_verdict(sha="a" * 40, pr=1, status="failed", failed_checks=["lint"])
    assert not reliability.gate(task_id=None, head_sha="a" * 40, requires_ci=True).ok
    reliability.record_ci_verdict(sha="b" * 40, pr=1, status="green")
    assert reliability.gate(task_id=None, head_sha="b" * 40, requires_ci=True).ok
    assert not reliability.gate(task_id=None, head_sha="c" * 40, requires_ci=True).ok


def test_green_new_head_resolves_ci_lineage(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    reliability.record_ci_verdict(sha="a" * 40, pr=1, status="failed", failed_checks=["coverage"])
    reliability.record_ci_verdict(sha="b" * 40, pr=1, status="green")
    state = json.loads(reliability.state_path().read_text())
    assert all(event["status"] == "resolved" for event in state["events"] if event["kind"] == "ci")


def test_notice_parser_keeps_failed_check_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    notice = (
        "CI abc1234 - 2 not green\n"
        "**CI / python-lint** — failure\n"
        "**CI / python-coverage** — failure"
    )
    record = reliability.record_ci_from_notice(sha="a" * 40, pr=7, notice=notice)
    assert record["status"] == "failed"
    assert record["failed_checks"] == ["CI / python-coverage", "CI / python-lint"]


def test_state_load_recovers_missing_and_corrupt_files(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    assert reliability.snapshot()["events"] == []
    path = reliability.state_path()
    path.write_text("{not-json", encoding="utf-8")
    assert reliability.snapshot() == {"version": 1, "events": [], "ci": {}}
    path.write_text("[]", encoding="utf-8")
    assert reliability.snapshot() == {"version": 1, "events": [], "ci": {}}


def test_state_load_adds_missing_top_level_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    path = reliability.state_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"events": []}', encoding="utf-8")
    state = reliability.snapshot()
    assert state["version"] == 1
    assert state["ci"] == {}


def test_git_common_dir_falls_back_on_git_failure(monkeypatch, tmp_path):
    class Result:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(reliability, "ROOT", tmp_path)
    monkeypatch.setattr(reliability.subprocess, "run", lambda *args, **kwargs: Result())
    assert reliability._git_common_dir() == tmp_path / ".git"


def test_fingerprint_normalizes_and_bounds_input():
    value = reliability.fingerprint("  alpha  ", "beta", "x" * 5000)
    assert len(value) == 20
    assert value == reliability.fingerprint("alpha", "beta", "x" * 5000)


def test_tool_failure_can_be_resolved_before_repeat(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    first = reliability.record_tool_failure(tool="Bash", command="cmd", summary="failure")
    path = reliability.state_path()
    state = json.loads(path.read_text(encoding="utf-8"))
    state["events"][0]["status"] = "resolved"
    path.write_text(json.dumps(state), encoding="utf-8")
    second = reliability.record_tool_failure(tool="Bash", command="cmd", summary="failure")
    assert first["repeat_count"] == 1
    assert second["repeat_count"] == 1


def test_invalid_ci_status_is_rejected():
    try:
        reliability.record_ci_verdict(sha="a" * 40, pr=1, status="bad")
    except ValueError as exc:
        assert "invalid CI status" in str(exc)
    else:
        raise AssertionError("invalid status was accepted")


def test_notice_parser_detects_green_and_pending(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    green = reliability.record_ci_from_notice(sha="a" * 40, pr=1, notice="All checks green")
    pending = reliability.record_ci_from_notice(sha="b" * 40, pr=1, notice="CI still running")
    assert green["status"] == "green"
    assert pending["status"] == "pending"


def test_notice_parser_uses_non_green_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    record = reliability.record_ci_from_notice(sha="a" * 40, pr=1, notice="CI result unavailable")
    assert record["status"] == "failed"
    assert record["failed_checks"] == ["non-green CI"]


def test_gate_short_circuits_when_ci_not_required(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    assert reliability.gate(task_id=None, head_sha=None, requires_ci=False).ok


def test_gate_requires_head_and_green_status(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    assert (
        "HEAD unavailable"
        in reliability.gate(task_id=None, head_sha=None, requires_ci=True).blockers[0]
    )
    reliability.record_ci_verdict(sha="a" * 40, pr=1, status="pending")
    result = reliability.gate(task_id=None, head_sha="a" * 40, requires_ci=True)
    assert not result.ok
    assert "pending" in result.blockers[0]


def test_gate_rejects_unresolved_repeat_for_same_task_and_head(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    sha = "a" * 40
    for _ in range(3):
        reliability.record_tool_failure(
            tool="Bash", command="cmd", summary="failure", task_id="task-1", head_sha=sha
        )
    reliability.record_ci_verdict(sha=sha, pr=1, status="green")
    result = reliability.gate(task_id="task-1", head_sha=sha, requires_ci=True)
    assert not result.ok
    assert "repeated failure" in result.blockers[0]


def test_active_context_handles_non_git_and_valid_git(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "ROOT", tmp_path)

    class Failed:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(reliability.subprocess, "run", lambda *args, **kwargs: Failed())
    assert reliability._active_context() == (None, None)

    class Ok:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, **kwargs):
        if command[-1] == "--show-toplevel":
            return Ok(str(tmp_path))
        if command[-1] == "--git-common-dir":
            return Ok(str(tmp_path))
        if command[-1] == "HEAD":
            return Ok("a" * 40)
        return Ok("")

    monkeypatch.setattr(reliability.subprocess, "run", fake_run)
    assert reliability._active_context()[1] == "a" * 40


def test_tool_failure_binds_active_context_when_not_supplied(monkeypatch, tmp_path):
    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    monkeypatch.setattr(reliability, "_active_context", lambda: ("task-1", "a" * 40))
    event = reliability.record_tool_failure(
        tool="Bash", command="cmd", summary="failure"
    )
    assert event["task_id"] == "task-1"
    assert event["head_sha"] == "a" * 40
