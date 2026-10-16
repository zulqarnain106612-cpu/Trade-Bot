"""The autonomous CI diagnostics collector replaces the old fail-fast notifier."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OLD_WORKFLOW = ROOT / ".github/workflows/ci-failure-notify.yml"
COLLECTOR_WORKFLOW = ROOT / ".github/workflows/ci-diagnostics.yml"


def _triggers(spec: dict) -> dict:
    # PyYAML treats a bare `on` key as boolean True under YAML 1.1.
    return spec.get("on") or spec.get(True) or {}


def test_old_fail_fast_workflow_is_retired() -> None:
    assert not OLD_WORKFLOW.exists(), "the old workflow cancels sibling checks and must not run"


def test_collector_is_autonomous_and_starts_only_after_source_completion() -> None:
    spec = yaml.safe_load(COLLECTOR_WORKFLOW.read_text(encoding="utf-8"))
    triggers = _triggers(spec)
    assert triggers["workflow_run"]["types"] == ["completed"]
    assert set(triggers["workflow_run"]["workflows"]) == {
        "CI",
        "CodeQL",
        "Security",
        "Workflow lint",
    }
    assert "pull_request" not in triggers
    assert "workflow_dispatch" not in triggers
    assert spec["jobs"]["collect"]["permissions"]["actions"] == "read"
    assert spec["jobs"]["collect"]["permissions"]["checks"] == "write"
    assert (
        "python3 scripts/ci_diagnostics.py collect" in spec["jobs"]["collect"]["steps"][-1]["run"]
    )


def test_collector_does_not_cancel_source_runs_or_publish_pr_comments() -> None:
    workflow = COLLECTOR_WORKFLOW.read_text(encoding="utf-8")
    collector = (ROOT / "scripts/ci_diagnostics.py").read_text(encoding="utf-8")
    assert "cancel-in-progress: false" in workflow
    assert "actions: write" not in workflow
    assert "cancelWorkflowRun" not in collector


def test_report_cli_is_the_single_agent_facing_route() -> None:
    policy = (ROOT / "config/command_policy.json").read_text(encoding="utf-8")
    cli = (ROOT / "scripts/prfail").read_text(encoding="utf-8")
    assert "./scripts/prfail --pr <PR_NUMBER>" in policy
    assert "from ci_diagnostics import main_prfail" in cli
