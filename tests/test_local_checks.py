from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "local_checks.py"


def load_module():
    spec = importlib.util.spec_from_file_location("local_checks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_ci_notice_paths_are_narrow():
    module = load_module()
    comment = """
    **CI / Python tests (shard 3/6)** — failure
    FAILED tests/test_event_loop_acquisition.py::test_no_source_file_calls_get_event_loop
    FAILED tests/test_selftest_rate_limit.py::test_it_is_keyed_by_client_ip
    """
    assert module.test_paths(comment) == [
        "tests/test_event_loop_acquisition.py",
        "tests/test_selftest_rate_limit.py",
    ]


def test_prepare_green_deletes_any_stale_plan(tmp_path, monkeypatch, capsys):
    module = load_module()
    module.PLAN = tmp_path / "plan.json"
    module.PLAN.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(
        module, "git", lambda *args: "abc123" if args == ("rev-parse", "HEAD") else ""
    )
    monkeypatch.setattr(
        module,
        "current_pr",
        lambda sha: {"number": 123},
    )
    monkeypatch.setattr(
        module,
        "check_runs",
        lambda sha: [
            {"name": "CI gate (all jobs green)", "status": "completed", "conclusion": "success"},
            {
                "name": "Security gate (all jobs green)",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "CodeQL gate (all jobs green)",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Workflow lint gate (all jobs green)",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Python (lint + governance docs)",
                "status": "completed",
                "conclusion": "success",
            },
            {"name": "Python tests (shard 1/6)", "status": "completed", "conclusion": "success"},
        ],
    )

    assert module.prepare() == 0
    assert not module.PLAN.exists()
    assert "local execution locked" in capsys.readouterr().out


def test_prepare_records_only_failed_checks(tmp_path, monkeypatch):
    module = load_module()
    module.PLAN = tmp_path / "plan.json"
    monkeypatch.setattr(module, "git", lambda *args: "abc123")
    monkeypatch.setattr(module, "current_pr", lambda sha: {"number": 123})
    monkeypatch.setattr(
        module,
        "check_runs",
        lambda sha: [
            {"name": "CI gate (all jobs green)", "status": "completed", "conclusion": "failure"},
            {
                "name": "Security gate (all jobs green)",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "CodeQL gate (all jobs green)",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Workflow lint gate (all jobs green)",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Python (lint + governance docs)",
                "status": "completed",
                "conclusion": "success",
            },
            {"name": "Python tests (shard 1/6)", "status": "completed", "conclusion": "failure"},
        ],
    )
    monkeypatch.setattr(
        module,
        "latest_notice",
        lambda pr, sha: "FAILED tests/test_event_loop_acquisition.py::test_no_source",
    )

    assert module.prepare() == 0
    plan = json.loads(module.PLAN.read_text(encoding="utf-8"))
    assert plan["failed_checks"] == ["ci-gate", "tests"]
    assert plan["test_paths"] == ["tests/test_event_loop_acquisition.py"]


def test_run_refuses_a_check_that_was_not_failed(tmp_path, monkeypatch):
    module = load_module()
    module.PLAN = tmp_path / "plan.json"
    module.PLAN.write_text(
        json.dumps({"source_sha": "abc123", "failed_checks": ["tests"], "test_paths": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "git", lambda *args: "def456")
    try:
        module.run_check("lint")
    except SystemExit as exc:
        assert "was not non-green" in str(exc)
    else:
        raise AssertionError("run_check should refuse an unrecorded check")
