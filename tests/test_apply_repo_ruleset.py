"""
Tests for the version-controlled repository ruleset and its apply script.

The ruleset is the merge policy. If it is wrong, nothing downstream notices:
a required check silently stops being required and pull requests keep merging.
So the file's content is asserted here as strictly as the script's logic.

Nothing here touches the network. The parts that talk to GitHub are separated
from the parts that decide what to send, and only the latter are tested.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "apply_repo_ruleset.py"
RULESET = PROJECT_ROOT / ".github" / "rulesets" / "main-protection.json"


def load_module():
    spec = importlib.util.spec_from_file_location("apply_repo_ruleset", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return load_module()


@pytest.fixture(scope="module")
def raw() -> dict:
    return json.loads(RULESET.read_text(encoding="utf-8"))


class TestRulesetFile:
    def test_is_valid_json(self, raw):
        assert isinstance(raw, dict)

    def test_targets_the_default_branch(self, raw):
        includes = raw["conditions"]["ref_name"]["include"]
        assert "~DEFAULT_BRANCH" in includes

    def test_enforcement_is_active(self, raw):
        # A ruleset in "evaluate" mode reports but does not block, which looks
        # identical to protection from the settings page.
        assert raw["enforcement"] == "active"

    def test_requires_status_checks(self, raw):
        types = {rule["type"] for rule in raw["rules"]}
        assert "required_status_checks" in types

    def test_requires_branch_to_be_up_to_date(self, raw, mod):
        rule = next(r for r in raw["rules"] if r["type"] == "required_status_checks")
        assert rule["parameters"]["strict_required_status_checks_policy"] is True

    def test_blocks_force_push_and_deletion(self, raw):
        types = {rule["type"] for rule in raw["rules"]}
        assert "non_fast_forward" in types
        assert "deletion" in types

    def test_requires_changes_to_arrive_by_pull_request(self, raw):
        types = {rule["type"] for rule in raw["rules"]}
        assert "pull_request" in types


class TestRequiredChecksMatchTheWorkflows:
    """
    The required contexts must name gates that actually exist.

    A typo here is invisible: GitHub happily accepts a required check that no
    workflow ever posts, and then every pull request waits forever -- or, if
    the rule is later relaxed, merges with nothing verified.
    """

    def gate_names(self) -> set[str]:
        import yaml

        names = set()
        for path in (PROJECT_ROOT / ".github" / "workflows").glob("*.yml"):
            spec = yaml.safe_load(path.read_text(encoding="utf-8"))
            gate = (spec.get("jobs") or {}).get("gate")
            if gate and gate.get("name"):
                names.add(gate["name"])
        return names

    def test_every_required_check_is_a_real_gate(self, mod, raw):
        required = set(mod.required_contexts(mod.strip_comments(raw)))
        assert required, "the ruleset requires no checks at all"
        unknown = required - self.gate_names()
        assert not unknown, f"required checks with no matching gate job: {unknown}"

    def test_the_correctness_gates_are_all_required(self, mod, raw):
        required = set(mod.required_contexts(mod.strip_comments(raw)))
        for expected in (
            "CI gate (all jobs green)",
            "Security gate (all jobs green)",
            "CodeQL gate (all jobs green)",
            "Workflow lint gate (all jobs green)",
        ):
            assert expected in required

    def test_the_advisory_review_gate_is_not_required(self, mod, raw):
        # CLAUDE.md and docs/CLOUD_REVIEW.md both say the cloud review is
        # advisory. If that changes, change those docs in the same commit.
        required = set(mod.required_contexts(mod.strip_comments(raw)))
        assert "Cloud review gate (all jobs green)" not in required


class TestStripComments:
    def test_underscore_keys_are_removed(self, mod):
        cleaned = mod.strip_comments({"a": 1, "_why": "x", "b": {"_c": 2, "d": 3}})
        assert cleaned == {"a": 1, "b": {"d": 3}}

    def test_lists_are_walked(self, mod):
        cleaned = mod.strip_comments({"rules": [{"type": "t", "_why": "x"}]})
        assert cleaned == {"rules": [{"type": "t"}]}

    def test_scalars_pass_through(self, mod):
        assert mod.strip_comments("text") == "text"
        assert mod.strip_comments(7) == 7

    def test_the_real_file_has_no_underscore_keys_after_stripping(self, mod, raw):
        cleaned = json.dumps(mod.strip_comments(raw))
        assert '"_' not in cleaned


class TestLoadRuleset:
    def test_loads_and_strips(self, mod):
        payload = mod.load_ruleset()
        assert payload["name"]
        assert "_comment" not in payload

    def test_rejects_a_ruleset_missing_required_keys(self, mod, tmp_path, monkeypatch):
        broken = tmp_path / "broken.json"
        broken.write_text(json.dumps({"name": "x"}), encoding="utf-8")
        monkeypatch.setattr(mod, "RULESET_PATH", broken)
        with pytest.raises(ValueError, match="missing required key"):
            mod.load_ruleset()

    def test_missing_file_raises(self, mod, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "RULESET_PATH", tmp_path / "absent.json")
        with pytest.raises(FileNotFoundError):
            mod.load_ruleset()


class TestResolveRepo:
    def test_explicit_value_wins(self, mod):
        assert mod.resolve_repo("owner/repo") == ("owner", "repo")

    def test_falls_back_to_the_environment(self, mod, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "env-owner/env-repo")
        assert mod.resolve_repo(None) == ("env-owner", "env-repo")

    def test_rejects_a_value_without_a_slash(self, mod, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        with pytest.raises(ValueError, match="owner/repo"):
            mod.resolve_repo("nope")


class TestCompare:
    """--check must report real drift and stay quiet about GitHub's own defaults."""

    def payload(self, contexts: list[str], enforcement: str = "active") -> dict:
        return {
            "enforcement": enforcement,
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {"required_status_checks": [{"context": c} for c in contexts]},
                }
            ],
        }

    def test_identical_payloads_have_no_problems(self, mod):
        one = self.payload(["A", "B"])
        assert mod.compare(one, dict(one)) == []

    def test_extra_remote_keys_are_ignored(self, mod):
        # GitHub echoes ids, timestamps and defaults the file never sets; a
        # deep equality check would call that drift and make --check useless.
        local = self.payload(["A"])
        remote = self.payload(["A"])
        remote["id"] = 12345
        remote["created_at"] = "2026-01-01T00:00:00Z"
        assert mod.compare(local, remote) == []

    def test_a_missing_required_check_is_reported(self, mod):
        problems = mod.compare(self.payload(["A", "B"]), self.payload(["A"]))
        assert any("B" in p and "not enforced" in p for p in problems)

    def test_an_unexpected_required_check_is_reported(self, mod):
        problems = mod.compare(self.payload(["A"]), self.payload(["A", "Z"]))
        assert any("Z" in p for p in problems)

    def test_disabled_enforcement_is_reported(self, mod):
        # Switching a ruleset to "evaluate" leaves it listed but blocking
        # nothing; that must not read as compliant.
        problems = mod.compare(self.payload(["A"]), self.payload(["A"], enforcement="evaluate"))
        assert any("enforcement" in p for p in problems)

    def test_a_missing_rule_type_is_reported(self, mod):
        local = self.payload(["A"])
        local["rules"].append({"type": "non_fast_forward"})
        problems = mod.compare(local, self.payload(["A"]))
        assert any("non_fast_forward" in p for p in problems)


class TestRequiredContexts:
    def test_returns_empty_when_no_such_rule(self, mod):
        assert mod.required_contexts({"rules": [{"type": "deletion"}]}) == []

    def test_reads_contexts_in_order(self, mod):
        payload = {
            "rules": [
                {
                    "type": "required_status_checks",
                    "parameters": {"required_status_checks": [{"context": "A"}, {"context": "B"}]},
                }
            ]
        }
        assert mod.required_contexts(payload) == ["A", "B"]


def test_required_gate_workflows_trigger_on_every_pull_request() -> None:
    """A required check must run on every pull request, or none of them merge.

    GitHub treats a required check that never reports as *pending*, not as
    absent: the pull request is blocked forever with no failing job to fix.
    So a workflow whose gate is named in `required_status_checks` may not
    path-scope its `pull_request:` trigger.

    This is not hypothetical. `workflow-lint.yml` filtered its pull-request
    trigger to `.github/workflows/**`, which made `Workflow lint gate (all
    jobs green)` -- a required context -- unreachable for every pull request
    that changed only source, and those pull requests sat blocked.
    """
    import re

    ruleset = json.loads(RULESET.read_text())
    required = {
        check["context"]
        for rule in ruleset["rules"]
        if rule["type"] == "required_status_checks"
        for check in rule["parameters"]["required_status_checks"]
    }

    workflows = PROJECT_ROOT / ".github" / "workflows"
    unreachable = []
    for path in sorted(workflows.glob("*.yml")):
        text = path.read_text()
        gates = {
            m.group(1).strip() for m in re.finditer(r"name:\s*(.*gate \(all jobs green\))", text)
        }
        if not gates & required:
            continue
        # The `pull_request:` block runs until the next key at the same indent.
        block = re.search(r"^  pull_request:\n((?:    .*\n|\n)*)", text, re.MULTILINE)
        if block is not None and re.search(r"^    paths(-ignore)?:", block.group(1), re.MULTILINE):
            unreachable.append(path.name)

    assert not unreachable, (
        "these workflows own a required gate but path-scope their pull_request "
        f"trigger, so the gate never reports and every unrelated PR is blocked: {unreachable}"
    )
