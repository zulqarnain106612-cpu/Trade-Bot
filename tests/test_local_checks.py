"""
The failed-PR-only local check wrapper: what prepare records, and what run
then agrees to execute.

Decides:
  - REG-0024 — A failure recorded by local_checks prepare is reproducible by local_checks run
"""

from __future__ import annotations

import importlib.util
import json
import sys
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
    monkeypatch.setattr(module, "git", lambda *args: "abc123")
    monkeypatch.setattr(module, "current_pr", lambda sha: {"number": 123})
    monkeypatch.setattr(module, "latest_notice", lambda pr, sha: "CI abc1234 — all checks green")
    monkeypatch.setattr(module, "STATE_DIR", tmp_path / "state")
    from src.agent_control import reliability

    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    assert module.prepare() == 0
    assert not module.PLAN.exists()
    assert "local execution locked" in capsys.readouterr().out


def test_prepare_records_only_failed_checks_from_notice(tmp_path, monkeypatch):
    module = load_module()
    module.PLAN = tmp_path / "plan.json"
    monkeypatch.setattr(module, "git", lambda *args: "abc123")
    monkeypatch.setattr(module, "current_pr", lambda sha: {"number": 123})
    monkeypatch.setattr(
        module,
        "latest_notice",
        lambda pr, sha: (
            "CI abc1234 — 2 not green\n"
            "**CI / CI gate (all jobs green)** — failure\n"
            "**CI / Python tests (shard 1/6)** — failure\n"
            "FAILED tests/test_event_loop_acquisition.py::test_no_source"
        ),
    )
    monkeypatch.setattr(module, "STATE_DIR", tmp_path / "state")
    from src.agent_control import reliability

    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    assert module.prepare() == 0
    plan = json.loads(module.PLAN.read_text(encoding="utf-8"))
    assert plan["failed_checks"] == ["ci-gate", "tests"]
    assert plan["test_paths"] == ["tests/test_event_loop_acquisition.py"]


def test_a_recorded_failure_is_runnable_by_the_key_run_accepts(tmp_path, monkeypatch):
    # REG-0024: prepare stored the CI display name while `run` only accepts
    # alias keys, so no recorded failure could ever be reproduced locally.
    module = load_module()
    module.PLAN = tmp_path / "plan.json"
    monkeypatch.setattr(module, "git", lambda *args: "abc123")
    monkeypatch.setattr(module, "current_pr", lambda sha: {"number": 123})
    monkeypatch.setattr(
        module,
        "latest_notice",
        lambda pr, sha: (
            "CI abc1234 — 2 not green\n"
            "**CI / Python (lint + governance docs)** — failure · `Lint`\n"
            "**CI / Python tests (shard 2/6)** — failure · `Run tests (sharded)`\n"
            "FAILED tests/test_event_loop_acquisition.py::test_no_source"
        ),
    )
    monkeypatch.setattr(module, "STATE_DIR", tmp_path / "state")
    from src.agent_control import reliability

    monkeypatch.setattr(reliability, "_git_common_dir", lambda: tmp_path)
    assert module.prepare() == 0
    recorded = json.loads(module.PLAN.read_text(encoding="utf-8"))["failed_checks"]
    assert recorded == ["lint", "tests"]
    assert set(recorded) <= set(module.ALIASES.values())

    ran = []
    monkeypatch.setattr(module, "command_run", lambda name, command, *a: ran.append(command) or 0)
    assert module.run_check("tests") == 0
    assert ran[0][-1] == "tests/test_event_loop_acquisition.py"


def test_lint_covers_every_file_the_branch_changes(tmp_path, monkeypatch):
    # REG-0024: CI formats the whole tree. A file the branch committed before
    # the failed run is as much a candidate as the fix on top of it; only a
    # file that no longer exists is left out.
    module = load_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    for name in ("early.py", "fix.py", "notes.md"):
        (tmp_path / name).write_text("", encoding="utf-8")
    outputs = {
        ("merge-base", "origin/main", "HEAD"): "base",
        ("diff", "--name-only", "base", "HEAD"): "early.py\nnotes.md\ngone.py",
        ("diff", "--name-only", "failed"): "fix.py",
        ("diff", "--name-only", "--cached"): "",
    }
    monkeypatch.setattr(module, "git", lambda *args: outputs[args])
    assert module.lint_paths("failed") == ["early.py", "fix.py"]


def test_the_script_can_import_the_repository_package(monkeypatch):
    # REG-0024: as a script, sys.path[0] is scripts/, never the repository root.
    monkeypatch.setattr(sys, "path", [p for p in sys.path if Path(p or ".").resolve() != ROOT])
    load_module()
    assert str(ROOT) in sys.path


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
