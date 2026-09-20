"""
The merge queue's one fatal misconfiguration, pinned.

A queue entry is tested on a temporary ref built from main plus every entry
ahead of it. The required checks must run on that ref; if one of them does not
trigger on `merge_group`, the entry waits for a result that can never arrive
and the queue jams for every pull request behind it. Nothing fails, so nothing
notifies -- it is a silent stall of exactly the kind the queue was adopted to
prevent.

So the rule is mechanical: whatever is required to merge must also run in the
queue. These tests derive the required set from the workflows themselves
rather than restating it, so adding a gate cannot quietly skip this check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = PROJECT_ROOT / ".github" / "workflows"

# Advisory, by CLAUDE.md: the cloud review never approves or merges, so it is
# deliberately not a required check and therefore not a queue participant.
ADVISORY = {"claude-review.yml"}

# Reports on a failed run of another workflow; it has no place in a queue.
NOT_A_GATE = {"ci-failure-notify.yml"}


def _triggers(spec: dict) -> dict:
    # PyYAML reads a bare `on:` key as the boolean True.
    return spec.get("on") or spec.get(True) or {}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def gating_workflows() -> list[Path]:
    """Workflows that gate a pull request, and so must also gate the queue."""
    out = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        if path.name in ADVISORY or path.name in NOT_A_GATE:
            continue
        if "pull_request" in _triggers(_load(path)):
            out.append(path)
    return out


# Mirrors the guard applied in .github/workflows/*.yml; see
# tests/test_pr_queue.py, which owns the requirement that it exists.
_DRAFT_GUARD = "!(github.event_name == 'pull_request' && github.event.pull_request.draft)"


class TestQueueParticipation:
    def test_there_are_gating_workflows_to_check(self):
        """Guards against the suite passing because it found nothing."""
        assert gating_workflows()

    @pytest.mark.parametrize("path", gating_workflows(), ids=lambda p: p.name)
    def test_a_gating_workflow_also_runs_in_the_queue(self, path):
        triggers = _triggers(_load(path))
        assert "merge_group" in triggers, (
            f"{path.name} gates a pull request but does not trigger on "
            "merge_group; queued entries would wait forever for its check."
        )

    @pytest.mark.parametrize("path", gating_workflows(), ids=lambda p: p.name)
    def test_the_queue_reaches_the_same_gate_job(self, path):
        """
        The gate is what branch protection requires, so the gate is what has
        to report on the queue ref. A workflow that triggers on merge_group
        but whose gate is fenced off by a pull-request-only condition is the
        same jam wearing a different hat.
        """
        spec = _load(path)
        gate = spec["jobs"].get("gate")
        if gate is None:
            pytest.skip(f"{path.name} has no gate job")
        condition = str(gate.get("if", ""))
        # The queue's draft guard names `pull_request`, but only to exclude
        # drafts: for any other event the guard is true, so the gate still
        # runs on a queue ref. Strip it before looking for a real fence.
        residue = condition.replace(_DRAFT_GUARD, "")
        assert "pull_request" not in residue, (
            f"{path.name}'s gate is conditioned on pull_request and would be "
            "skipped in the merge queue."
        )


class TestTransientRefsAreNotPublished:
    def test_codeql_does_not_upload_from_a_queue_ref(self):
        """
        A queue ref is as temporary as a pull request ref.

        Uploading analysis from it would attribute results to a branch that
        ceases to exist the moment the entry merges or is dropped.
        """
        spec = _load(WORKFLOWS / "codeql.yml")
        uploads = [
            str(step.get("with", {}).get("upload", ""))
            for job in spec["jobs"].values()
            for step in job.get("steps", [])
            if isinstance(step, dict) and "upload" in (step.get("with") or {})
        ]
        assert uploads, "codeql.yml no longer declares an upload condition"
        for condition in uploads:
            assert "merge_group" in condition
