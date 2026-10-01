"""GOV-029: a registry conflict between independent additions resolves itself.

The common case is two pull requests that each register one entry: git
conflicts on the shared array tail although the data does not. The resolver
must keep both, and must refuse -- not guess -- when both sides changed the
same entry differently.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "resolve_registry_merge.py"
_spec = importlib.util.spec_from_file_location("resolve_registry_merge", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _e(entry_id: str, title: str = "t") -> dict:
    return {"id": entry_id, "title": title}


def _doc(*entries: dict, version: int = 1) -> dict:
    return {"registry_version": version, "entries": list(entries)}


BASE = _doc(_e("A"), _e("B"))


def test_both_sides_adding_an_entry_keeps_both() -> None:
    ours = _doc(_e("A"), _e("B"), _e("GOV-036"))
    theirs = _doc(_e("A"), _e("B"), _e("SEC-0006"))
    merged = mod.merge(BASE, ours, theirs)
    assert [e["id"] for e in merged["entries"]] == ["A", "B", "SEC-0006", "GOV-036"]


@pytest.mark.parametrize(
    ("ours", "theirs", "expected"),
    [
        (_doc(_e("A", "ours"), _e("B")), BASE, "ours"),
        (BASE, _doc(_e("A", "theirs"), _e("B")), "theirs"),
        (_doc(_e("A", "same"), _e("B")), _doc(_e("A", "same"), _e("B")), "same"),
    ],
    ids=["ours-only", "theirs-only", "identical"],
)
def test_an_entry_changed_on_one_side_takes_that_side(ours, theirs, expected) -> None:
    merged = mod.merge(BASE, ours, theirs)
    assert merged["entries"][0]["title"] == expected


def test_a_deletion_on_one_side_holds_when_the_other_did_not_touch_it() -> None:
    merged = mod.merge(BASE, _doc(_e("A")), BASE)
    assert [e["id"] for e in merged["entries"]] == ["A"]


@pytest.mark.parametrize(
    ("ours", "theirs"),
    [
        (_doc(_e("A", "x"), _e("B")), _doc(_e("A", "y"), _e("B"))),
        (_doc(_e("A")), _doc(_e("A"), _e("B", "edited"))),
        (_doc(_e("A"), _e("B"), _e("C", "x")), _doc(_e("A"), _e("B"), _e("C", "y"))),
    ],
    ids=["both-edited", "deleted-vs-edited", "same-new-id"],
)
def test_conflicting_changes_are_refused_not_guessed(ours, theirs) -> None:
    with pytest.raises(mod.Unresolvable):
        mod.merge(BASE, ours, theirs)


def test_top_level_keys_follow_the_one_side_rule() -> None:
    merged = mod.merge(BASE, _doc(_e("A"), _e("B"), version=2), BASE)
    assert merged["registry_version"] == 2


def test_main_writes_the_merged_document_in_the_files_own_indent(tmp_path, monkeypatch) -> None:
    # main() reads stages 1-3 from git's index during a conflicted merge;
    # _stage is the only git call, so it is replaced rather than spawning git.
    stages = {
        1: BASE,
        2: _doc(_e("A"), _e("B"), _e("GOV-036")),
        3: _doc(_e("A"), _e("B"), _e("SEC-0006")),
    }
    monkeypatch.setattr(mod, "_stage", lambda n, _p: (stages[n], json.dumps(stages[n], indent=2)))
    path = tmp_path / "registry.json"
    assert mod.main(["resolve_registry_merge.py", str(path)]) == 0
    merged = json.loads(path.read_text())
    assert [e["id"] for e in merged["entries"]] == ["A", "B", "SEC-0006", "GOV-036"]
    assert path.read_text().startswith('{\n  "registry_version"')


def test_main_writes_nothing_when_unresolvable(tmp_path, monkeypatch) -> None:
    stages = {1: BASE, 2: _doc(_e("A", "x"), _e("B")), 3: _doc(_e("A", "y"), _e("B"))}
    monkeypatch.setattr(mod, "_stage", lambda n, _p: (stages[n], json.dumps(stages[n])))
    path = tmp_path / "registry.json"
    assert mod.main(["resolve_registry_merge.py", str(path)]) == 1
    assert not path.exists()
