"""Regression tests for deterministic codebase knowledge index maintenance.

Decides:
  - GOV-066 — knowledge indexes have a deterministic integrity checker and safe refresh path
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "codebase_knowledge_index.py"
SPEC = importlib.util.spec_from_file_location("codebase_knowledge_index", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
indexer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(indexer)


def _facts(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest()[:8], len(data.splitlines())


def _table_row(path: str, count: int, digest: str) -> str:
    return f"| 1 | P0 | {path} | Python | {count} | {digest} | preserve this note |"


def test_refresh_updates_metadata_and_preserves_notes(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("first = 1\nsecond = 2\n", encoding="utf-8")
    digest, count = _facts(source)
    original = (
        "# File Index\n\n"
        "## Top 50 reference files\n"
        "| # | Priority | Path | Type | Lines | SHA8 | Why it matters |\n"
        + _table_row("src/example.py", 1, "00000000")
        + "\n\n## Complete tracked-file index\n"
        "| # | Priority | Path | Type | Lines | SHA8 | Notes |\n"
        "| ---: | --- | --- | --- | ---: | --- | --- |\n"
        + _table_row("src/example.py", 1, "00000000")
        + "\n"
    )
    monkeypatch.setattr(indexer, "tracked_paths", lambda root: {"src/example.py"})

    refreshed, issues = indexer.refresh_file_index(original, tmp_path)

    assert not issues
    assert refreshed.count(f"| {count} | {digest} | preserve this note |") == 2
    assert "preserve this note" in refreshed
    assert "00000000" not in refreshed


def test_refresh_refuses_to_hide_missing_tracked_paths(tmp_path: Path, monkeypatch) -> None:
    index = (
        "## Complete tracked-file index\n"
        "| # | Priority | Path | Type | Lines | SHA8 | Notes |\n"
        "| ---: | --- | --- | --- | ---: | --- | --- |\n"
        + _table_row("src/missing.py", 1, "00000000")
        + "\n"
    )
    monkeypatch.setattr(indexer, "tracked_paths", lambda root: {"src/missing.py", "new.py"})

    _, issues = indexer.refresh_file_index(index, tmp_path)

    assert any(
<<<<<<< HEAD
        "tracked path is missing from the working tree: src/missing.py" in issue
        for issue in issues
    )
    assert any(
        "tracked path is missing from the working tree: new.py" in issue
        for issue in issues
    )
=======
        "tracked path is missing from the working tree: src/missing.py" in issue for issue in issues
    )
    assert any("tracked path is missing from the working tree: new.py" in issue for issue in issues)
>>>>>>> origin/main


def test_anchor_validation_reports_stale_hash_and_out_of_range(tmp_path: Path) -> None:
    source = tmp_path / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("first = 1\n", encoding="utf-8")
    digest, _ = _facts(source)
    (tmp_path / "codebase-analysis-docs").mkdir()
    (tmp_path / "codebase-analysis-docs" / "CHUNK_INDEX.md").write_text(
<<<<<<< HEAD
        "## src/example.py\n\nSHA8 00000000; total lines 9.\n"
        "- [[F:src/example.py#1-2#00000000]]\n",
=======
        "## src/example.py\n\nSHA8 00000000; total lines 9.\n- [[F:src/example.py#1-2#00000000]]\n",
>>>>>>> origin/main
        encoding="utf-8",
    )
    master = f"Stable source: [[F:src/example.py#1-2#{digest}]]"

    issues = indexer.validate_anchors(master, tmp_path)

    assert any("stale CHUNK_INDEX metadata" in issue for issue in issues)
    assert any("stale CHUNK_INDEX.md anchor hash" in issue for issue in issues)
    assert any("invalid CHUNK_INDEX.md anchor range" in issue for issue in issues)
    assert any("invalid CODEBASE_KNOWLEDGE.md anchor range" in issue for issue in issues)

<<<<<<< HEAD
=======

>>>>>>> origin/main
def test_api_reference_must_match_source_route_and_line() -> None:
    source = '@app.get("/status")\nasync def status():\n    return {"ok": True}\n'
    valid_reference = (
        "## All routes\n"
        "| Method | Path | Handler | Line | Dependencies | Arguments |\n"
        "| --- | --- | --- | ---: | --- | --- |\n"
        "| GET | /status | status | 2 | | |\n"
    )

    assert indexer.validate_api_reference(source, valid_reference) == []

    stale_reference = valid_reference.replace("/status", "/stale")
    issues = indexer.validate_api_reference(source, stale_reference)

    assert len(issues) == 1
    assert "route inventory differs" in issues[0]


<<<<<<< HEAD
def test_refresh_adds_new_tracked_paths_and_updates_totals(
    tmp_path: Path, monkeypatch
) -> None:
=======
def test_refresh_adds_new_tracked_paths_and_updates_totals(tmp_path: Path, monkeypatch) -> None:
>>>>>>> origin/main
    existing = tmp_path / "src" / "existing.py"
    existing.parent.mkdir()
    existing.write_text("value = 1\n", encoding="utf-8")
    new_file = tmp_path / "tests" / "new_test.py"
    new_file.parent.mkdir()
    new_file.write_text("def test_new():\n    assert True\n", encoding="utf-8")
    digest, count = _facts(existing)
    index = (
        "# File Index\n"
        "- Tracked paths: 1\n"
        "- Python files: 1\n"
        "- P0 anchors: 1\n"
        "- P1 files: 0\n"
        "## Complete tracked-file index\n"
        "| # | Priority | Path | Type | Lines | SHA8 | Notes |\n"
        "| ---: | --- | --- | --- | ---: | --- | --- |\n"
<<<<<<< HEAD
        + f"| 1 | P0 | src/existing.py | Python | {count} | {digest} | existing note |\n"
=======
        + (f"| 1 | P0 | src/existing.py | Python | {count} | {digest} | existing note |\n")
>>>>>>> origin/main
    )
    monkeypatch.setattr(
        indexer,
        "tracked_paths",
        lambda root: {"src/existing.py", "tests/new_test.py"},
    )

    refreshed, issues = indexer.refresh_file_index(index, tmp_path)

    assert not issues
    assert "- Tracked paths: 2" in refreshed
    assert "- Python files: 2" in refreshed
    assert "- P0 anchors: 1" in refreshed
    assert "- P1 files: 1" in refreshed
    assert "tests/new_test.py" in refreshed
    assert "existing note" in refreshed
