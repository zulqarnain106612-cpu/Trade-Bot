"""Every script under scripts/ must resolve first-party imports on its own.

`python scripts/ingest_repo.py` puts *scripts/* on sys.path, never the repo
root, so a bare `from rag_mongo.db import ...` raises ModuleNotFoundError.
That is not hypothetical: the RAG ingest workflow, whose only step is
`python scripts/ingest_repo.py`, failed on main with exactly that error the
first time it ran. Anything invoked as a path -- workflows, cron, a shell --
hits it, while `python -m scripts.x` and pytest do not, which is why it
survived review.

AST-only: nothing here imports the scripts, several of which open a MongoDB
connection or load an embedding model at import time.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

# Top-level packages that only exist relative to the repo root.
FIRST_PARTY = {
    "common",
    "kg",
    "orchestrator",
    "rag_mongo",
    "review",
    "scripts",
    "src",
    "tools",
}


def _python_scripts() -> list[Path]:
    return sorted(p for p in SCRIPTS.glob("*.py") if p.name != "__init__.py")


def _first_party_import_line(tree: ast.Module) -> int | None:
    """Line of the earliest top-level import of a first-party package."""
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level == 0 and root in FIRST_PARTY:
                return node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FIRST_PARTY:
                    return node.lineno
    return None


def _is_repo_root_bootstrap(node: ast.stmt) -> bool:
    """True for `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`.

    The anchor must be __file__. `sys.path.insert(0, ".")` -- what
    ingest_missing.py used to do -- passes an import run from the repo root
    and fails from anywhere else, so it deliberately does not count.
    """
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    call = node.value
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "insert":
        return False
    target = call.func.value
    if not (
        isinstance(target, ast.Attribute)
        and target.attr == "path"
        and isinstance(target.value, ast.Name)
        and target.value.id == "sys"
    ):
        return False
    return any(isinstance(sub, ast.Name) and sub.id == "__file__" for sub in ast.walk(call))


@pytest.mark.parametrize("script", _python_scripts(), ids=lambda p: p.name)
def test_first_party_imports_follow_a_repo_root_bootstrap(script: Path) -> None:
    tree = ast.parse(script.read_text(), filename=str(script))
    import_line = _first_party_import_line(tree)
    if import_line is None:
        pytest.skip(f"{script.name} imports nothing first-party")

    bootstrap_lines = [n.lineno for n in tree.body if _is_repo_root_bootstrap(n)]
    assert bootstrap_lines, (
        f"{script.name} imports a first-party package at line {import_line} but never "
        f"puts the repo root on sys.path. Running it as `python {script.relative_to(REPO)}` "
        f"will raise ModuleNotFoundError."
    )
    assert min(bootstrap_lines) < import_line, (
        f"{script.name} inserts the repo root on sys.path at line {min(bootstrap_lines)}, "
        f"after its first first-party import at line {import_line}."
    )
