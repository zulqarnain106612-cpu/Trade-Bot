#!/usr/bin/env python3
"""
Resolve the RBAC double-implementation conflict that every branch in the
session-opus stack hits when merging origin/main.

Both sides implemented v8 role-based access control independently: the stack
named the role dependency `current_role` and put the key->role mapping in
`auth.resolve_role`; main named the dependency `resolve_role` and folded the
mapping into `auth.verify_api_key`. main's version is the one that shipped
(PR #37), so it is authoritative everywhere the two disagree — resolving the
other way would re-diverge every remaining branch from the merge target.

Two hunk classes need different handling:

  * plain hunks -> take main's side verbatim.
  * `from X import a, b` hunks -> union the names. Taking main's side there
    would silently drop an import the branch's own (non-RBAC) code still
    uses, which the conflict markers give no hint about; a union is correct
    for an import list by construction and any genuinely unused name is
    caught by ruff F401 in CI.

Whole-file `--theirs` is used for the files whose only branch-side change was
the superseded RBAC work, listed in TAKE_THEIRS.

Usage (from a conflicted merge, repo root):  uv run scripts/resolve_rbac_stack_merge.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


TAKE_THEIRS = (
    ".secrets.baseline",
    "src/api/auth.py",
    "src/api/access_control.py",
    "tests/test_order_fsm_registry.py",
    "tests/test_risk_controls_api.py",
    "tests/test_self_tuning_api.py",
)

_IMPORT_RE = re.compile(r"^from ([\w.]+) import (.+)$")


def _conflicted() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def _union_imports(ours: list[str], theirs: list[str]) -> list[str] | None:
    """Merge two single-line `from X import ...` sides, or None if not that shape."""
    if len(ours) != 1 or len(theirs) != 1:
        return None
    a, b = _IMPORT_RE.match(ours[0].strip()), _IMPORT_RE.match(theirs[0].strip())
    if not a or not b or a.group(1) != b.group(1):
        return None
    names = sorted({n.strip() for n in (a.group(2) + "," + b.group(2)).split(",") if n.strip()})
    return [f"from {a.group(1)} import {', '.join(names)}"]


def _drop_shadowed_imports(lines: list[str]) -> list[str]:
    """
    Remove imported names that the same file also defines at module level.

    The union rule can re-introduce a name main deliberately stopped
    importing because it moved that definition into this module — exactly
    the `resolve_role` case. Importing it then shadows the local `def` and
    ruff rejects the file (F811). A name the module defines itself always
    wins over the import.
    """
    defined = {m.group(1) for m in (re.match(r"^(?:def|class) (\w+)", line) for line in lines) if m}
    out = []
    for line in lines:
        m = _IMPORT_RE.match(line)
        if m and defined.intersection(n.strip() for n in m.group(2).split(",")):
            kept = [n.strip() for n in m.group(2).split(",") if n.strip() not in defined]
            if not kept:
                continue
            line = f"from {m.group(1)} import {', '.join(kept)}"
        out.append(line)
    return out


def _resolve_text(text: str) -> tuple[str, int]:
    """Return (resolved text, hunks resolved)."""
    lines, out, hunks = text.splitlines(), [], 0
    i = 0
    while i < len(lines):
        if not lines[i].startswith("<<<<<<< "):
            out.append(lines[i])
            i += 1
            continue
        i += 1
        ours: list[str] = []
        while not lines[i].startswith("======="):
            ours.append(lines[i])
            i += 1
        i += 1
        theirs: list[str] = []
        while not lines[i].startswith(">>>>>>> "):
            theirs.append(lines[i])
            i += 1
        i += 1
        out.extend(_union_imports(ours, theirs) or theirs)
        hunks += 1
    if hunks:
        out = _drop_shadowed_imports(out)
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), hunks


def _repoint_auth_test() -> str | None:
    """
    Retarget tests/test_api_auth_middleware.py at main's auth API.

    This module merges *cleanly* — git sees no conflict — yet it imports
    `resolve_role` from src.api.auth, which only the stack's auth.py ever
    defined. Keeping main's auth.py therefore leaves a clean-merged file
    referencing a symbol that no longer exists, and an ImportError at
    collection takes down the entire test session, not just this module.
    Main folded that mapping into verify_api_key, which has the same
    signature and return type, so the call sites transfer unchanged.
    """
    p = Path("tests/test_api_auth_middleware.py")
    if not p.exists():
        return None
    text = p.read_text()
    if "resolve_role" not in text:
        return None
    text = text.replace("    resolve_role,\n", "").replace("resolve_role(", "verify_api_key(")
    p.write_text(text)
    return str(p)


def main() -> int:
    conflicted = _conflicted()
    if not conflicted:
        print("no conflicted paths — nothing to do", file=sys.stderr)
        return 1

    staged: list[str] = []
    for path in conflicted:
        if path in TAKE_THEIRS:
            subprocess.run(["git", "checkout", "--theirs", "--", path], check=True)
            print(f"{path}: took main's version whole")
        else:
            p = Path(path)
            resolved, hunks = _resolve_text(p.read_text())
            p.write_text(resolved)
            print(f"{path}: resolved {hunks} hunk(s)")
        staged.append(path)

    if repointed := _repoint_auth_test():
        print(f"{repointed}: repointed at main's verify_api_key")
        staged.append(repointed)

    subprocess.run(["git", "add", "--", *staged], check=True)
    print(f"\nstaged {len(staged)} file(s); review `git diff --cached` before committing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
