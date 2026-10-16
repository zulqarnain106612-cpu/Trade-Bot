<<<<<<< HEAD
"""
REG-0018 — the CI notice reports one actionable non-green check, not a
consolidated list of failures and cancellation fallout.

Decides:
  - REG-0018 — fail fast, cancel sibling workflows, and keep one current notice
=======
"""The diagnostics report is terminal-state complete and bound to the current PR head.

Decides:
  - REG-0018 — CI diagnostics wait for terminal outcomes without cancelling sibling workflows
>>>>>>> origin/main
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci-diagnostics.yml"
COLLECTOR = ROOT / "scripts/ci_diagnostics.py"


<<<<<<< HEAD
@pytest.fixture(scope="module")
def script() -> str:
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in spec["jobs"]["notify"]["steps"]:
        body = step.get("with", {}).get("script")
        if body and "ci-notice" in body:
            return body
    pytest.fail("notify step with the ci-notice script not found")


def test_every_non_success_conclusion_is_actionable(script: str) -> None:
    assert "const isBad = (j) => j.conclusion !== 'success';" in script
    assert "skipped" in script
    assert "neutral" in script
    assert "failure" in script


def test_first_non_green_check_cancels_other_in_flight_workflows(script: str) -> None:
    assert "const actionable = failing;" in script
    assert "cancelWorkflowRun" in script
    assert "failing.splice(0, failing.length, actionable[0]);" in script
    assert "latest.size >= WATCHED.length" in script
    assert "poll < 90" in script


def test_only_one_check_is_published(script: str) -> None:
    assert "const selected = failing[0] || null;" in script
    assert "failing.slice(0, MAX_JOBS)" not in script
    assert "const MAX_JOBS" not in script
    assert "workflow Succeeded" in script


def test_current_head_is_bound_and_stale_runs_are_ignored(script: str) -> None:
    assert "ci-notice-sha:" in script
    assert "currentPr.data.head.sha !== headSha" in script
    assert "Ignoring stale CI notice" in script


@pytest.mark.parametrize(
    "epilogue",
    [
        "Canceling since a higher priority waiting request",
        "No files were found with the provided path:",
    ],
)
def test_cancellation_epilogues_are_treated_as_noise(script: str, epilogue: str) -> None:
    assert epilogue in script
    noise = script[script.index("const NOISE = [") : script.index("const isNoise")]
    assert epilogue in noise


def test_the_notice_has_no_advice_or_log_link(script: str) -> None:
    body_start = script.index("const marker = '<!-- ci-notice -->';")
    body = script[body_start : script.index("const text = body.join")]
    assert "details_url" not in body
    assert "html_url" not in body
=======
def test_collector_waits_for_every_applicable_workflow_and_job() -> None:
    source = COLLECTOR.read_text(encoding="utf-8")
    assert 'run.get("status") != "completed"' in source
    assert "_all_jobs_terminal(jobs, run)" in source
    assert "COLLECT_DEADLINE_SECONDS" in source
    assert "report remains unfinalized" in source


def test_collector_has_no_manual_dispatch_or_agent_triggered_collection() -> None:
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
    assert re.search(r'"pr": pr_number,\s*"head_sha": sha', source)
    assert "_ensure_head_unchanged(slug, pr_number, sha)" in source
    assert "master_external_id" in source or "_external_id(pr_number, sha)" in source
    assert "report is stale or belongs to a different pull request" in source


def test_collector_retains_outcomes_and_source_locations() -> None:
    source = COLLECTOR.read_text(encoding="utf-8")
    for field in (
        "start_line",
        "start_column",
        "end_line",
        "end_column",
        "workflow_run_id",
        "job_id",
    ):
        assert field in source
    assert "_add_structured_outcomes" in source
    assert "_add_annotations" in source
    assert "_add_log_diagnostics" in source


def test_report_retrieval_has_one_canonical_invocation() -> None:
    from scripts.ci_diagnostics import _canonical_invocation

    cli = (ROOT / "scripts/prfail").read_text(encoding="utf-8")
    assert "main_prfail" in cli
    assert _canonical_invocation(["--pr", "462"]) == 462
    assert _canonical_invocation(["--run-id", "1"]) is None
    assert _canonical_invocation(["--pr", "462", "--run-id", "1"]) is None
>>>>>>> origin/main
