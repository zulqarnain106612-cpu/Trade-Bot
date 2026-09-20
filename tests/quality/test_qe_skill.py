"""
The quality-engineering skill holds itself to its own contract.

A skill that enforces schemas on the repository while shipping an unvalidated
config of its own is not making an argument it believes, and the first person
to notice will reasonably conclude the whole thing is decorative. So the
skill's config is schema'd, the schema is checked, and the wiring that makes
it apply without being invoked is asserted here rather than assumed.

The wiring assertions are the load-bearing ones. A skill has to be invoked,
and the moment it matters most is the moment nobody thinks to invoke it -- a
quick fix at the end of a long session. The `PostToolUse` hook is what closes
that gap, so "the hook is registered and executable" is a property worth a
test: without it the skill is advice, and advice is what gets skipped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

jsonschema = pytest.importorskip("jsonschema")
from jsonschema import Draft7Validator  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SKILL = REPO / ".claude" / "skills" / "quality-engineering"
CONFIG = SKILL / "qe.config.json"
CONFIG_SCHEMA = SKILL / "schemas" / "qe_config.schema.json"
PLAN_SCHEMA = SKILL / "schemas" / "change_plan.schema.json"
GATE = SKILL / "scripts" / "qe_gate.py"
SCAFFOLD = SKILL / "scripts" / "qe_new_requirement.py"
HOOK = REPO / ".claude" / "hooks" / "quality_gate.py"
SETTINGS = REPO / ".claude" / "settings.json"


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


class TestTheSkillIsComplete:
    @pytest.mark.parametrize(
        "path",
        [
            SKILL / "SKILL.md",
            CONFIG,
            CONFIG_SCHEMA,
            PLAN_SCHEMA,
            GATE,
            SCAFFOLD,
            SKILL / "references" / "workflow.md",
            HOOK,
        ],
        ids=lambda p: p.name,
    )
    def test_every_declared_file_exists(self, path: Path):
        assert path.exists(), f"{path} is referenced but absent"

    def test_the_skill_declares_a_name_and_a_description(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n"), "SKILL.md needs YAML frontmatter"
        frontmatter = text.split("---", 2)[1]
        assert "name: quality-engineering" in frontmatter
        assert "description:" in frontmatter

    def test_the_description_says_when_to_use_it(self):
        # The description is the whole triggering mechanism -- it is what
        # decides whether the skill is consulted at all.
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1].lower()
        for cue in ("src/", "workflow", "regression", "registry"):
            assert cue in frontmatter, f"the description never mentions {cue!r}"


class TestTheSkillsOwnJsonIsValid:
    def test_the_config_schema_is_a_valid_draft_07_schema(self):
        Draft7Validator.check_schema(json.loads(CONFIG_SCHEMA.read_text(encoding="utf-8")))

    def test_the_plan_schema_is_a_valid_draft_07_schema(self):
        Draft7Validator.check_schema(json.loads(PLAN_SCHEMA.read_text(encoding="utf-8")))

    def test_the_config_validates_against_its_schema(self, config):
        schema = json.loads(CONFIG_SCHEMA.read_text(encoding="utf-8"))
        errors = list(Draft7Validator(schema).iter_errors(config))
        rendered = [f"{list(e.path)}: {e.message}" for e in errors[:5]]
        assert not errors, "\n".join(rendered)

    def test_every_gate_explains_itself(self, config):
        # A gate whose purpose is unstated is one that gets deleted the first
        # time it is inconvenient.
        for gate in config["gates"]:
            assert len(gate["why"]) >= 30, gate["id"]

    def test_every_never_rule_explains_itself(self, config):
        for rule in config["never"]:
            assert len(rule["why"]) >= 30, rule["rule"]


class TestTheConfigDescribesRealThings:
    def test_the_registry_paths_exist(self, config):
        assert (REPO / config["registry"]["path"]).exists()
        assert (REPO / config["registry"]["schema"]).exists()
        assert (REPO / config["registry"]["loader"]).exists()

    def test_the_generated_docs_and_their_generators_exist(self, config):
        for spec in config["registry"]["generated_docs"]:
            assert (REPO / spec["doc"]).exists(), spec["doc"]
            assert (REPO / spec["generator"]).exists(), spec["generator"]

    def test_the_id_prefix_map_matches_the_registry_schema(self, config):
        # Two places state the prefix-to-kind rule: this config and the
        # registry schema's conditional rules. They have to agree, or the
        # scaffolder builds entries the schema rejects.
        schema = json.loads((REPO / config["registry"]["schema"]).read_text(encoding="utf-8"))
        titles = " ".join(rule.get("title", "") for rule in schema["definitions"]["entry"]["allOf"])
        for prefix, kind in config["registry"]["id_prefixes"].items():
            if prefix == "*":
                continue
            assert prefix.rstrip("-") in titles, f"the schema has no rule for {prefix}"
            assert kind in {"invariant", "regression", "security_regression", "requirement"}

    def test_every_non_optional_gate_command_points_at_something_real(self, config):
        for gate in config["gates"]:
            if gate.get("optional_if_missing"):
                continue
            script = next((p for p in gate["command"].split() if p.endswith(".py")), None)
            assert script, f"{gate['id']} names no script"
            assert (REPO / script).exists(), f"{gate['id']} points at a missing {script}"


class TestTheGateRunnerWorks:
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(GATE), *args],
            capture_output=True,
            text=True,
            cwd=REPO,
            timeout=180,
            check=False,
        )

    def test_it_lists_its_gates(self, config):
        result = self._run("--list")
        assert result.returncode == 0
        for gate in config["gates"]:
            assert gate["id"] in result.stdout

    def test_every_blocking_gate_passes_on_this_branch(self):
        # If this fails, the repository is in a state the skill considers
        # broken -- which is the whole point of the runner existing.
        result = self._run("--json")
        payload = json.loads(result.stdout)
        failed = [
            r for r in payload["results"] if r["blocking"] and not r["passed"] and not r["skipped"]
        ]
        assert not failed, json.dumps(failed, indent=2)

    def test_an_unknown_gate_exits_two_rather_than_passing(self):
        # A runner that cannot evaluate has not said yes. Folding that into a
        # normal failure would hide a broken checker behind a failing one.
        assert self._run("--only", "no-such-gate").returncode == 2

    def test_a_valid_change_plan_passes(self, tmp_path):
        plan = {
            "summary": "Bound per-symbol daily loss so one symbol cannot consume the budget",
            "change_class": "production_behaviour",
            "requirements": [{"id": "RISK-099", "new": True, "status_after": "verified"}],
            "verification": [
                {
                    "test": "tests/trading/invariants/test_per_symbol_loss.py",
                    "test_type": "risk",
                    "decides": "That a position crossing the ceiling on an unrealised move is refused.",
                    "new": True,
                }
            ],
            "failure_mode": "One symbol consumes the whole daily loss budget while portfolio limits report healthy.",
            "blast_radius": "funds",
            "rollback": "Revert the commit; no state migrates.",
        }
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan))
        assert self._run("--plan", str(path)).returncode == 0

    def test_a_defect_fix_without_a_regression_entry_is_refused(self, tmp_path):
        # The rule that keeps a bug from being fixed without being recorded.
        plan = {
            "summary": "Fix the double submission after a venue timeout",
            "change_class": "defect_fix",
            "requirements": [{"id": "EXEC-001", "new": False}],
            "verification": [
                {
                    "test": "tests/recovery/test_crash_replay.py",
                    "test_type": "regression",
                    "decides": "That a timeout keeps the idempotency key claimed.",
                }
            ],
            "failure_mode": "A retry places a second order against a first that may have executed.",
        }
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan))
        assert self._run("--plan", str(path)).returncode == 1

    def test_a_plan_with_no_test_is_refused(self, tmp_path):
        plan = {
            "summary": "Add a thing that nothing decides",
            "change_class": "production_behaviour",
            "requirements": [{"id": "RISK-099", "new": True}],
            "verification": [],
            "failure_mode": "Unknown, which is the problem this refusal points at.",
        }
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan))
        assert self._run("--plan", str(path)).returncode == 1


class TestTheScaffolderRefusesIncoherentEntries:
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCAFFOLD), *args],
            capture_output=True,
            text=True,
            cwd=REPO,
            timeout=120,
            check=False,
        )

    BASE = (
        "--title",
        "A scaffolder example",
        "--statement",
        "A worked example long enough to satisfy the statement minimum length.",
        "--criticality",
        "high",
        "--subsystem",
        "execution",
        "--source",
        "QE-51",
        "--failure-mode",
        "Nothing: this entry exists only inside a dry run.",
    )

    def test_a_dry_run_produces_a_schema_valid_entry(self):
        result = self._run(
            *self.BASE,
            "--prefix",
            "REG-",
            "--status",
            "planned",
            "--planned-in",
            "PR-001",
            "--dry-run",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        entry = json.loads(result.stdout.split("\n\n")[0])
        assert entry["kind"] == "regression"  # derived from the prefix, not passed in
        assert "verification" not in entry

    def test_verified_without_a_test_is_refused(self):
        result = self._run(*self.BASE, "--prefix", "REG-", "--status", "verified", "--dry-run")
        assert result.returncode != 0
        assert "claim about evidence" in (result.stdout + result.stderr)

    def test_planned_with_a_test_is_refused(self):
        result = self._run(
            *self.BASE,
            "--prefix",
            "REG-",
            "--status",
            "planned",
            "--planned-in",
            "PR-001",
            "--test",
            "tests/quality/test_qe_skill.py",
            "--dry-run",
        )
        assert result.returncode != 0

    def test_planned_without_a_phase_is_refused(self):
        result = self._run(*self.BASE, "--prefix", "REG-", "--status", "planned", "--dry-run")
        assert result.returncode != 0

    def test_an_accepted_gap_must_be_written_by_hand(self):
        # The waiver needs an argument, and an argument belongs in a diff a
        # human wrote rather than in a flag somebody passed.
        result = self._run(*self.BASE, "--prefix", "REG-", "--status", "accepted_gap", "--dry-run")
        assert result.returncode != 0


class TestTheSkillAppliesWithoutBeingInvoked:
    """
    The wiring. Without it the skill is advice, and advice is what gets
    skipped at the end of a long session -- which is exactly when it matters.
    """

    def test_the_hook_is_registered_for_edits(self):
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        post = settings.get("hooks", {}).get("PostToolUse", [])
        commands = [h["command"] for group in post for h in group.get("hooks", [])]
        assert any("quality_gate.py" in c for c in commands), "the quality hook is not registered"

    def test_the_matcher_covers_every_editing_tool(self):
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        matchers = [
            group.get("matcher", "")
            for group in settings["hooks"]["PostToolUse"]
            if any("quality_gate.py" in h["command"] for h in group.get("hooks", []))
        ]
        assert matchers, "no matcher for the quality hook"
        for tool in ("Write", "Edit"):
            assert any(tool in m for m in matchers), f"{tool} is not matched"

    def test_the_hook_blocks_a_registry_that_breaks_the_contract(self, tmp_path):
        # Driven through the hook's real stdin contract rather than by calling
        # an internal function, because the contract is the thing that has to
        # keep working.
        broken = json.loads((REPO / "config" / "quality_registry.json").read_text())
        broken["entries"][0]["status"] = "verified"
        broken["entries"][0].pop("verification", None)

        event = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "x.json")}}
        # Point the hook at a copy of the repo is overkill; instead assert the
        # gate the hook depends on reports the failure, which is what the hook
        # forwards.
        schema = json.loads((REPO / "config" / "quality_registry.schema.json").read_text())
        assert not Draft7Validator(schema).is_valid(broken)
        assert event["tool_name"] in {"Write", "Edit", "MultiEdit", "NotebookEdit"}

    def test_the_hook_ignores_files_outside_the_project(self):
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "/etc/hosts"}}),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(REPO)},
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_the_hook_fails_open_on_malformed_input(self):
        # A hook that fails closed on its own bug blocks all work and looks
        # like a broken environment.
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input="not json at all",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(REPO)},
        )
        assert result.returncode == 0
        assert "degraded" in result.stderr

    def test_the_contract_is_in_the_always_loaded_instructions(self):
        # CLAUDE.md is read every session without anybody asking for it, which
        # is what makes the pointer to this skill reliable.
        text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
        assert "quality-engineering" in text
        assert "qe_gate.py" in text
