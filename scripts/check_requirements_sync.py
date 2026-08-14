#!/usr/bin/env python3
"""Fail when the requirements files drift apart.

CI installs ``requirements.lock``, which pip-compile builds from
``requirements.in`` alone. A package declared only in ``requirements.txt`` is
therefore never installed anywhere while looking perfectly well declared —
which is exactly how duckdb reached main and made every DuckDBStore test fail
with ModuleNotFoundError. Nothing caught it, so this does.

Checks:
  1. every package in requirements.txt is also in requirements.in
  2. every package in requirements.in is pinned in requirements.lock
  3. every lock entry carries a --hash (CI installs with --require-hashes)
  4. requirements-optional.txt does not overlap requirements.in (an extra is
     either optional or required, and the lazy-import fallbacks assume the
     former)

None of this catches a lock compiled under the wrong interpreter: pip-compile
resolves per Python version, so the lock must be built on the same 3.11 CI
uses. Compiling on 3.14 silently drops 3.11-only transitive deps (coincurve)
and pins 3.12+ wheels (numpy 2.5.0), both of which only fail in CI.

Run: python scripts/check_requirements_sync.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

# name, optionally [extras], up to the first version specifier / marker / comment
_REQ = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;].*)?$")


def normalize(name: str) -> str:
    """PEP 503 canonical form: py_ecc, py-ecc and PY.ECC are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse(path: Path) -> set[str]:
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        # skip blanks, pip flags (-r, --hash, --index-url) and hash continuations
        if not line or line.startswith("-"):
            continue
        match = _REQ.match(line)
        if match:
            names.add(normalize(match.group(1)))
    return names


def unhashed_lock_entries(path: Path) -> list[str]:
    """Pinned lock entries carrying no --hash.

    CI installs the lock with --require-hashes, which rejects the whole file if
    a single requirement lacks a hash. Catch it here rather than in a CI run.
    """
    bad: list[str] = []
    pending: str | None = None
    hashed = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.split("#", 1)[0].strip().rstrip("\\").strip()
        if not stripped:
            continue
        if stripped.startswith("--hash="):
            hashed = True
            continue
        if stripped.startswith("-"):  # --index-url and friends
            continue
        if "==" in stripped:
            if pending is not None and not hashed:
                bad.append(pending)
            pending, hashed = stripped.split("==", 1)[0].strip(), False
    if pending is not None and not hashed:
        bad.append(pending)
    return bad


def main() -> int:
    req_in = parse(ROOT / "requirements.in")
    req_txt = parse(ROOT / "requirements.txt")
    req_lock = parse(ROOT / "requirements.lock")
    req_opt = parse(ROOT / "requirements-optional.txt")

    errors: list[str] = []

    if missing := sorted(req_txt - req_in):
        errors.append(
            "declared in requirements.txt but not requirements.in — these are "
            "never installed by CI (the lock is compiled from requirements.in "
            f"only): {', '.join(missing)}"
        )

    if unlocked := sorted(req_in - req_lock):
        errors.append(
            "in requirements.in but absent from requirements.lock — re-run "
            "`pip-compile --generate-hashes requirements.in -o requirements.lock`: "
            f"{', '.join(unlocked)}"
        )

    if unhashed := unhashed_lock_entries(ROOT / "requirements.lock"):
        errors.append(
            "pinned in requirements.lock with no --hash — CI installs with "
            "--require-hashes and rejects the entire file over one of these: "
            f"{', '.join(sorted(unhashed))}"
        )

    if both := sorted(req_opt & req_in):
        errors.append(
            "listed as both optional and required — pick one; the lazy-import "
            f"fallbacks assume optional: {', '.join(both)}"
        )

    if errors:
        print("requirements drift detected:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print(
        f"requirements in sync ({len(req_in)} direct, {len(req_lock)} locked, "
        f"{len(req_opt)} optional)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
