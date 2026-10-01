"""
REG-0018 — a cancelled job is not a failing job in the CI notice.

`.github/workflows/ci-failure-notify.yml` is the only channel this project
permits for CI failure information (CLAUDE.md section 9), so its precision
is load-bearing in a way an ordinary workflow's is not: there is no
fallback to reading the logs, and a notice that over-reports trains the
reader to skim the one thing they are allowed to read.

It over-reported. A push that replaces a commit cancels that commit's
in-flight runs, and every cancelled job was counted as "not green" -- one
observed notice read "11 not green" for two real failures and nine runs
cancelled by their own successor, padded with the artifact-upload warning
that a cancelled shard leaves behind.

Asserted against the workflow source. The classification is inline
JavaScript inside the YAML, so there is no module to import; this is the
same shape as tests/test_ci_workflow_cost.py, which reads workflow text
for the same reason.
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
    """The notify step's inline script, read once for the module."""
    spec = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in spec["jobs"]["notify"]["steps"]:
        body = step.get("with", {}).get("script")
        if body and "ci-notice" in body:
            return body
    pytest.fail("notify step with the ci-notice script not found")


def test_cancelled_jobs_are_partitioned_out_of_the_count(script: str) -> None:
    """The headline count must be real failures, not failures plus fallout."""
    assert "f.job.conclusion === 'cancelled'" in script
    assert "const failures = failing.filter" in script
    assert "${failures.length} not green" in script


def test_a_cancelled_run_is_never_reported_as_green(script: str) -> None:
    """
    The opposite error, and the worse one: a commit whose jobs were
    cancelled proved nothing, so dropping cancellations from the count must
    not let the notice claim the commit passed.
    """
    green = "— all checks green"
    assert "failures.length === 0 && cancelled.length === 0" in script
    # The green branch is guarded by both counts, never by failures alone.
    assert script.count(green) == 1
    guard = script[: script.index(green)]
    assert "cancelled.length === 0" in guard.rsplit("if (", 1)[-1]


def test_cancellations_are_still_named(script: str) -> None:
    """
    Silently dropping them would hide that the commit has no verdict, which
    is a different thing from having passed.
    """
    assert "superseded by a newer commit, no verdict" in script
    assert "— no verdict" in script


@pytest.mark.parametrize(
    "epilogue",
    [
        "Canceling since a higher priority waiting request",
        "No files were found with the provided path:",
    ],
)
def test_cancellation_epilogues_are_treated_as_noise(script: str, epilogue: str) -> None:
    """
    Both lines describe the cancellation, not a defect in the commit. They
    were quoted verbatim as if they were failure messages.
    """
    assert epilogue in script
    noise = script[script.index("const NOISE = [") : script.index("const isNoise")]
    assert epilogue in noise


def test_the_notice_still_carries_no_advice_or_log_link(script: str) -> None:
    """
    Section 9 again: the comment is failure information only. A change that
    adds a link to a log nobody may open would defeat the channel.
    """
    body_start = script.index("body.push(marker)")
    body = script[body_start : script.index("const text = body.join")]
    assert "details_url" not in body
    assert "html_url" not in body
