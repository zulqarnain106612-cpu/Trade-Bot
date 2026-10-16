"""The diagnostics report is terminal-state complete and bound to the current PR head."""
from __future__ import annotations

from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci-diagnostics.yml"
COLLECTOR = ROOT / "scripts/ci_diagnostics.py"


def test_collector_waits_for_every_applicable_workflow_and_job() -> None:
    source = COLLECTOR.read_text(encoding="utf-8")
    assert 'if not run or run.get("status") != "completed"' in source
    assert "_all_jobs_terminal(jobs, run)" in source
    assert "COLLECT_DEADLINE_SECONDS" in source
    assert "report remains unfinalized" in source


def test_collector_has_no_manual_dispatch_or_agent-triggered_collection() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on") or workflow.get(True) or {}
    source = COLLECTOR.read_text(encoding="utf-8")
    assert set(triggers) == {"workflow_run"}
    assert 'event_name == "workflow_run"' in source
    assert 'event_name == "workflow_dispatch"' not in source


def test_source_workflows_are_not_cancelled_by_diagnostics() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert "actions: write" not in WORKFLOW.read_text(encoding="utf-8")
    assert "cancelWorkflowRun" not in COLLECTOR.read_text(encoding="utf-8")


def test_report_identity_is_bound_to_pr_and_head_sha() -> None:
    source = COLLECTOR.read_text(encoding="utf-8")
    assert '"pr": pr_number, "head_sha": sha' in source
    assert "_ensure_head_unchanged(slug, pr_number, sha)" in source
    assert "master_external_id" in source or "_external_id(pr_number, sha)" in source
    assert "report is stale or belongs to a different pull request" in source


def test_collector_retains_outcomes_and_source_locations() -> None:
    source = COLLECTOR.read_text(encoding="utf-8")
    for field in ("start_line", "start_column", "end_line", "end_column", "workflow_run_id", "job_id"):
        assert field in source
    assert "_add_structured_outcomes" in source
    assert "_add_annotations" in source
    assert "_add_log_diagnostics" in source


def test_report_retrieval_has_one_canonical_invocation() -> None:
    cli = (ROOT / "scripts/prfail").read_text(encoding="utf-8")
    assert "main_prfail" in cli
    assert "--pr" in cli
    assert "--run-id" not in cli
