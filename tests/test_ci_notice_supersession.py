"""
REG-0018 — the CI notice reports one actionable non-green check, not a
consolidated list of failures and cancellation fallout.

Decides:
  - REG-0018 — fail fast, cancel sibling workflows, and keep one current notice
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci-failure-notify.yml"
)


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
