"""
REG-0010: a registry id is allocated against every branch, not one work tree.

`qe_new_requirement.py` used to pick the next id by scanning
`config/quality_registry.json` in the current work tree, which sees only what
has merged to main. Two branches open at once were handed the same id three
times in one session; every collision was caught by a human reading the diff,
and nothing in CI would have caught any of them. On merge the result is two
requirements sharing an id, or a renumber that silently breaks a `depends_on`
pointing at the old one.

The same run also showed the scaffolder printing `[ok  ] added SEC-0001` for
an entry the loader then refused: it validated against the JSON schema, which
does not know the test-type taxonomy. A scaffolder that declares success for
something the gate rejects moves the failure to CI and makes the entry look
reviewed on the way past.

Git is faked at `_git`, so these tests spawn nothing and touch no repository.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / ".claude" / "skills" / "quality-engineering" / "scripts"
sys.path.insert(0, str(SCRIPTS))

pytest.importorskip("jsonschema")

import qe_gate  # noqa: E402
import qe_new_requirement as scaffold  # noqa: E402
import qe_registry_refs as refs  # noqa: E402


def _entry(entry_id: str, title: str = "A title long enough to pass") -> dict:
    return {
        "id": entry_id,
        "kind": "regression" if entry_id.startswith("REG-") else "requirement",
        "title": title,
        "statement": "A statement that is long enough to satisfy the schema minimum.",
        "criticality": "high",
        "subsystem": "governance",
        "status": "planned",
        "source": "QE-99",
        "failure_mode": "Something goes wrong in production, at length.",
        "planned_in": "PR-001",
    }


def fake_git(refs_by_blob: dict[str, list[dict]], *, base_ids: list[str] | None = None):
    """
    A `_git` stand-in over an in-memory {blob sha: entries} world.

    Blob shas are the dict keys; every ref named in `refs_by_blob` resolves to
    its blob. `base_ids` is what `merge-base` resolves to, under the sha
    "BASE".
    """
    blobs = {sha: json.dumps({"entries": entries}) for sha, entries in refs_by_blob.items()}
    if base_ids is not None:
        blobs["BASE"] = json.dumps({"entries": [_entry(i) for i in base_ids]})

    def _run(repo, *args, stdin=None):
        if args[0] == "for-each-ref":
            return "".join(f"refs/heads/{sha}\n" for sha in refs_by_blob)
        if args[0] == "merge-base":
            return "BASE\n"
        if args[0] == "cat-file" and args[1].startswith("--batch-check"):
            out = []
            for line in (stdin or "").splitlines():
                key = line.split(":")[0].removeprefix("refs/heads/")
                out.append(f"{key} blob\n" if key in blobs else f"{line} missing\n")
            return "".join(out)
        if args[0] == "cat-file" and args[1] == "blob":
            return blobs[args[2]]
        raise AssertionError(f"unexpected git call: {args}")

    return _run


class TestIdAllocation:
    def test_an_id_taken_only_on_another_branch_is_not_handed_out_again(self, monkeypatch):
        # The work tree stops at REG-0009; a branch nobody has merged holds
        # REG-0010. Scanning the work tree alone hands out REG-0010 twice.
        monkeypatch.setattr(
            refs, "_git", fake_git({"other": [_entry("REG-0009"), _entry("REG-0010")]})
        )
        registry = {"entries": [_entry("REG-0009")]}
        taken = scaffold.taken_ids(registry)
        assert "REG-0010" in taken
        assert scaffold.next_id(taken, "REG-") == "REG-0011"

    def test_local_only_reproduces_the_old_colliding_behaviour(self, monkeypatch):
        # The escape hatch exists for a detached tree with no refs to read;
        # it is the defect on purpose, so it must stay opt-in.
        monkeypatch.setattr(refs, "_git", fake_git({"other": [_entry("REG-0010")]}))
        registry = {"entries": [_entry("REG-0009")]}
        assert scaffold.next_id(scaffold.taken_ids(registry, local_only=True), "REG-") == "REG-0010"

    def test_a_missing_git_warns_rather_than_refusing_to_allocate(self, monkeypatch):
        def explode(*_args, **_kwargs):
            raise refs.GitUnavailable("not a work tree")

        monkeypatch.setattr(refs, "_git", explode)
        taken = scaffold.taken_ids({"entries": [_entry("REG-0009")]})
        assert taken == {"REG-0009": "the working tree"}

    def test_an_explicit_id_held_by_another_branch_is_refused(self, monkeypatch, capsys):
        monkeypatch.setattr(refs, "_git", fake_git({"other": [_entry("REG-0042")]}))
        with pytest.raises(SystemExit) as caught:
            scaffold.main(_argv(entry_id="REG-0042"))
        assert "already exists on refs/heads/other" in str(caught.value)
        capsys.readouterr()


class TestScaffolderValidatesWithTheLoader:
    def test_a_test_type_the_schema_allows_and_the_loader_refuses_fails(self, monkeypatch, capsys):
        # `governance` is a plausible-looking test type and not a declared
        # one. The schema has no opinion; the gate's loader does.
        monkeypatch.setattr(refs, "_git", fake_git({}))
        code = scaffold.main(
            _argv(
                entry_id="REG-9999",
                extra=[
                    "--status",
                    "verified",
                    "--test",
                    "tests/quality/test_qe_id_allocation.py",
                    "--test-type",
                    "governance",
                ],
            )
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "test taxonomy" in out

    def test_a_declared_test_type_is_accepted(self, monkeypatch, capsys):
        monkeypatch.setattr(refs, "_git", fake_git({}))
        code = scaffold.main(
            _argv(
                entry_id="REG-9999",
                extra=[
                    "--status",
                    "verified",
                    "--test",
                    "tests/quality/test_qe_id_allocation.py",
                    "--test-type",
                    "regression",
                ],
            )
        )
        capsys.readouterr()
        assert code == 0


class TestCollisionGate:
    CONFIG = {
        "registry": {"path": "config/quality_registry.json", "loader": "src/quality/registry.py"}
    }

    def test_the_same_id_with_a_different_body_on_a_live_branch_is_reported(self, monkeypatch):
        monkeypatch.setattr(
            qe_gate.refs,
            "_git",
            fake_git(
                {"other": [_entry("REG-0010", "A different requirement entirely")]}, base_ids=[]
            ),
        )
        monkeypatch.setattr(
            qe_gate, "_read_registry_entries", lambda _p: [_entry("REG-0010")], raising=False
        )
        result = qe_gate.gate_registry_id_collision(self.CONFIG)
        assert not result.passed
        assert not result.blocking  # advisory: a stale branch must not block a merge
        assert "REG-0010" in result.findings[0]

    def test_the_same_id_with_the_same_body_is_the_same_entry(self, monkeypatch):
        # A branch that was updated from this one carries an identical entry.
        # That is one requirement in two places, not two requirements.
        monkeypatch.setattr(
            qe_gate.refs, "_git", fake_git({"other": [_entry("REG-0010")]}, base_ids=[])
        )
        monkeypatch.setattr(
            qe_gate, "_read_registry_entries", lambda _p: [_entry("REG-0010")], raising=False
        )
        assert qe_gate.gate_registry_id_collision(self.CONFIG).passed

    def test_a_branch_that_allocates_nothing_is_not_examined(self, monkeypatch):
        monkeypatch.setattr(
            qe_gate.refs, "_git", fake_git({"other": [_entry("REG-0010")]}, base_ids=["REG-0010"])
        )
        monkeypatch.setattr(
            qe_gate, "_read_registry_entries", lambda _p: [_entry("REG-0010")], raising=False
        )
        result = qe_gate.gate_registry_id_collision(self.CONFIG)
        assert result.passed and "no new ids" in result.detail


def _argv(*, entry_id: str, extra: list[str] | None = None) -> list[str]:
    return [
        "--id",
        entry_id,
        "--title",
        "A title long enough to pass",
        "--statement",
        "A statement that is long enough to satisfy the schema minimum.",
        "--criticality",
        "high",
        "--subsystem",
        "governance",
        "--source",
        "QE-99",
        "--failure-mode",
        "Something goes wrong in production, at length.",
        "--dry-run",
        *(extra or ["--status", "planned", "--planned-in", "PR-001"]),
    ]
