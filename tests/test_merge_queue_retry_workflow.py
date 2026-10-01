"""GOV-037: a pull request the merge queue drops is retried, then handed over.

The workflow runs with a write-capable token in the base branch's context, so
the properties that keep it safe are asserted alongside the ones that make it
useful: it never checks out or runs the pull request's code, never acts on a
fork, and stops after a fixed number of attempts with one mention of the owner.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "merge-queue-retry.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
SPEC = yaml.safe_load(TEXT)
TRIGGERS = SPEC.get("on") or SPEC.get(True)
JOB = SPEC["jobs"]["retry"]
SCRIPT = JOB["steps"][0]["with"]["script"]


def test_it_runs_on_every_event_that_changes_queue_eligibility() -> None:
    assert set(TRIGGERS) == {"pull_request_target"}
    assert set(TRIGGERS["pull_request_target"]["types"]) == {
        "opened",
        "reopened",
        "ready_for_review",
        "synchronize",
        "dequeued",
    }


def test_it_never_checks_out_or_runs_pull_request_code() -> None:
    uses = [step.get("uses", "") for step in JOB["steps"]]
    assert all(u.startswith("actions/github-script@") for u in uses)
    assert not any("run" in step for step in JOB["steps"])
    # The gate's checkout takes no ref: under pull_request_target that is the
    # base branch. Any ref, or any mention of the head, would be the head's code.
    for job in SPEC["jobs"].values():
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert "ref" not in step.get("with", {})
    assert "pull_request.head.sha" not in TEXT and "pull_request.head.ref" not in TEXT


def test_forks_are_never_auto_merged() -> None:
    assert JOB["if"] == "github.event.pull_request.head.repo.full_name == github.repository"


def test_it_acts_with_a_token_whose_events_start_workflows() -> None:
    assert "secrets.PR_AUTOUPDATE_TOKEN" in JOB["steps"][0]["with"]["github-token"]
    assert "core.setFailed" in SCRIPT and "Nothing changed" in SCRIPT


@pytest.mark.parametrize(
    "fragment",
    [
        "const MAX_RETRIES = 3;",
        "if (done >= MAX_RETRIES)",
        "body: `@${owner} have a look on this PR`",
        "if (labels.includes(EXHAUSTED)) return;",
        "if (labels.includes(HOLD))",
        "enqueuePullRequest(",
        "enablePullRequestAutoMerge(",
        "mergeMethod: SQUASH",
    ],
)
def test_the_retry_contract_is_in_the_script(fragment: str) -> None:
    assert fragment in SCRIPT


def test_new_content_starts_a_new_series_of_attempts() -> None:
    i = SCRIPT.index("if (action === 'synchronize')")
    assert "dropLabel" in SCRIPT[i : i + 300]


def test_its_gate_allows_only_the_fork_skip() -> None:
    gate = SPEC["jobs"]["gate"]
    assert gate["needs"] == ["retry"]
    assert gate["steps"][-1]["env"]["ALLOW_SKIPPED"] == "retry"
