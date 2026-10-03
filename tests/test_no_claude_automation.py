"""GOV-036: Claude runs only in sessions the owner starts and can see.

Every Claude run is billed to the owner's plan. A workflow that invokes Claude
(the removed claude-review.yml ran up to 120 turns on every PR push), or a
session that schedules its own wake-ups or subscribes to PR activity, spends
that plan with nobody watching. These tests keep both doors shut.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((PROJECT_ROOT / ".github" / "workflows").glob("*.y*ml"))
SETTINGS = json.loads((PROJECT_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))

# Anything that lets a workflow run Claude on the owner's account.
FORBIDDEN = re.compile(
    r"anthropics/claude-code-action"
    r"|CLAUDE_CODE_OAUTH_TOKEN"
    r"|ANTHROPIC_API_KEY"
    r"|\bclaude\s+(-p|--print)\b",
    re.IGNORECASE,
)

# Tools through which a session wakes itself later or is woken by GitHub.
UNATTENDED_TOOLS = {
    "ScheduleWakeup",
    "CronCreate",
    "RemoteTrigger",
    "mcp__github__subscribe_pr_activity",
}


def test_workflows_are_found() -> None:
    assert WORKFLOWS, "no workflow files found; the scan below would pass vacuously"


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_runs_claude(workflow: Path) -> None:
    match = FORBIDDEN.search(workflow.read_text(encoding="utf-8"))
    assert match is None, f"{workflow.name} invokes Claude via {match.group(0)!r}"


def test_the_removed_review_workflow_stays_removed() -> None:
    assert not (PROJECT_ROOT / ".github" / "workflows" / "claude-review.yml").exists()


def test_unattended_scheduling_tools_are_denied() -> None:
    denied = set(SETTINGS.get("permissions", {}).get("deny", []))
    missing = UNATTENDED_TOOLS - denied
    assert not missing, f".claude/settings.json no longer denies {sorted(missing)}"
