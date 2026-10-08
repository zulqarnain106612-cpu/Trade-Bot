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
