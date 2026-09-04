"""Every module under src/security/ must state a post-quantum posture.

The Crypto Architect law gate (LAW12) asks src/security/ and src/ecc/ for
a post-quantum reference, but `scripts/arch_gate.sh` exits 0 on MEDIUM
findings -- it annotates, it does not block. Three modules under
src/security/ had been sitting on that finding, so the requirement was
documented and unenforced.

src/ecc/ is deliberately out of scope here: every one of its modules is
recorded in config/arch_baseline.json as an accepted LAW12 finding. That
is a decision already taken, and a test is not the place to reopen it.
This covers the directory where the finding was live.

What is being pinned is that the question was *answered*, not that a
particular answer was given. Two of the modules concluded no migration
applies (constant_time.py has no asymmetric cryptography; credential_vault.py
derives symmetrically because hardened BIP-32 needs no public key), and that
is a perfectly good posture -- it is the silence that was the problem.

AST-only: nothing here imports the modules, which pull in `cryptography`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# LAW12 scopes itself to src/security/ and src/ecc/ in validate_arch.py;
# only the former is unbaselined. See the module docstring.
SCOPED_DIRS = ("src/security",)

# The same alternation LAW12 matches on, so this test and the gate cannot
# drift into disagreeing about what counts as a posture.
PQC_MARKERS = (
    "ML-KEM",
    "ML_KEM",
    "ML-DSA",
    "ML_DSA",
    "SLH-DSA",
    "SLH_DSA",
    "Dilithium",
    "Kyber",
    "pqc_",
    "post_quantum",
)


def _scoped_modules() -> list[Path]:
    out: list[Path] = []
    for d in SCOPED_DIRS:
        out.extend(p for p in (REPO / d).glob("*.py") if p.name != "__init__.py")
    return sorted(out)


@pytest.mark.parametrize("module", _scoped_modules(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_module_documents_a_post_quantum_posture(module: Path) -> None:
    tree = ast.parse(module.read_text(), filename=str(module))
    docstring = ast.get_docstring(tree) or ""

    if any(marker in module.read_text() for marker in PQC_MARKERS):
        # A module that references PQC in its code satisfies LAW12 on its own.
        return

    assert any(marker in docstring for marker in PQC_MARKERS), (
        f"{module.relative_to(REPO)} neither uses a post-quantum primitive nor "
        f"documents why it does not need one. State the posture in the module "
        f"docstring -- including 'no migration applies' where that is the honest "
        f"answer -- and say what would change it."
    )
