#!/usr/bin/env python3
"""
Resolve a merge conflict in config/quality_registry.json entry by entry.

Two branches that each register something append an entry at the end of the
same array, so git sees both edits touch the same lines and stops. The content
does not actually conflict: each side added a different id. Taking either side
textually loses the other's entry, and fusing the hunks produces a document the
strict loader refuses. This resolves the conflict the way the data means it --
a three-way merge per entry id, using the three versions git keeps in the index
during a conflicted merge (stage 1 base, 2 ours, 3 theirs).

Per entry id:
  * changed on one side only        -> that side's version
  * changed identically on both     -> that version
  * changed differently on both     -> unresolvable, exit 1, write nothing
  * added on one side               -> kept (ours appended after theirs' order)
  * added on both with one id       -> unresolvable unless identical
  * deleted on one side, untouched  -> deleted
  * deleted on one side, edited on the other -> unresolvable

Top-level keys other than "entries" follow the same one-side-changed rule.

Usage (inside a conflicted merge, from the repository root):
    python3 scripts/resolve_registry_merge.py [path]

Stdlib only: it runs in pr-auto-update.yml, which installs nothing (GOV-015).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

DEFAULT_PATH = "config/quality_registry.json"
_MISSING = object()


class Unresolvable(Exception):
    """Both sides changed the same thing differently; a person must decide."""


def _pick(key: str, base: object, ours: object, theirs: object) -> object:
    if ours == theirs:
        return ours
    if ours == base:
        return theirs
    if theirs == base:
        return ours
    raise Unresolvable(key)


def merge(base: dict, ours: dict, theirs: dict) -> dict:
    """Three-way merge of registry documents. Raises Unresolvable."""
    result: dict = {}
    for key in [*theirs, *(k for k in ours if k not in theirs)]:
        if key == "entries":
            continue
        value = _pick(
            key, base.get(key, _MISSING), ours.get(key, _MISSING), theirs.get(key, _MISSING)
        )
        if value is not _MISSING:
            result[key] = value

    b = {e["id"]: e for e in base.get("entries", [])}
    o = {e["id"]: e for e in ours.get("entries", [])}
    t = {e["id"]: e for e in theirs.get("entries", [])}
    order = [*t, *(i for i in o if i not in t)]
    entries = []
    for entry_id in order:
        value = _pick(
            entry_id,
            b.get(entry_id, _MISSING),
            o.get(entry_id, _MISSING),
            t.get(entry_id, _MISSING),
        )
        if value is not _MISSING:
            entries.append(value)
    result["entries"] = entries
    return result


def _stage(n: int, path: str) -> tuple[dict, str]:
    raw = subprocess.run(
        ["git", "show", f":{n}:{path}"], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(raw), raw


def _indent(raw: str) -> int:
    for line in raw.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped:
            return len(line) - len(stripped)
    return 2


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else DEFAULT_PATH
    try:
        base, _ = _stage(1, path)
        ours, _ = _stage(2, path)
        theirs, raw = _stage(3, path)
    except subprocess.CalledProcessError:
        print(f"{path} is not in a conflicted merge state.", file=sys.stderr)
        return 1
    try:
        merged = merge(base, ours, theirs)
    except Unresolvable as exc:
        print(f"{path}: '{exc.args[0]}' was changed differently on both sides.", file=sys.stderr)
        return 1
    Path(path).write_text(
        json.dumps(merged, indent=_indent(raw), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
