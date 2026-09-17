"""
The traceability matrix is generated, so it must never be edited by hand.

A hand-edited generated file is the worst of both worlds: it reads as
authoritative and is no longer derived from anything. These tests are the same
check CI runs (`--check`), plus the assertions that make the failure legible
when someone does edit the prose.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "generate_quality_docs.py"
OUTPUT = PROJECT_ROOT / "docs" / "quality" / "REQUIREMENTS_TRACEABILITY.md"


@pytest.fixture(scope="module")
def document() -> str:
    return OUTPUT.read_text(encoding="utf-8")


def test_the_generator_exists() -> None:
    assert SCRIPT.exists()


def test_the_document_is_not_stale() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_it_warns_against_hand_editing(document: str) -> None:
    assert "GENERATED FILE — DO NOT EDIT BY HAND" in document
    assert "config/quality_registry.json" in document


def test_it_names_the_regeneration_command(document: str) -> None:
    assert "python3 scripts/generate_quality_docs.py" in document


def test_every_registry_id_appears(document: str) -> None:
    from src.quality.registry import load_registry

    registry = load_registry()
    missing = [e.id for e in registry if f"`{e.id}`" not in document]
    assert not missing


def test_the_governance_rules_are_stated(document: str) -> None:
    # The document is where a reviewer learns what the registry refuses. If
    # these sentences go missing, the rules become folklore again.
    for phrase in (
        "must exist on disk",
        "name the PR that",
        "can **never** be an `accepted_gap`",
    ):
        assert phrase in document
