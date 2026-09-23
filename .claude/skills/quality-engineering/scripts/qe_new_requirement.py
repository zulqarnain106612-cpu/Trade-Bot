#!/usr/bin/env python3
"""
Add a registry entry that is schema-valid by construction.

Hand-editing `config/quality_registry.json` is how entries arrive with a
`planned_in` on a verified status, a kind that disagrees with the id prefix,
or a `verification` list on something planned. Every one of those is caught by
the schema, but being caught means a round trip -- and the entry that survives
several round trips tends to be the one edited until the validator stops
complaining rather than the one that says something true.

So this builds the entry from the rules instead: the kind follows from the id
prefix, the status decides whether `planned_in` or `verification` is present,
and the result is validated before it is written.

    qe_new_requirement.py --id REG-0042 --title "..." --statement "..." \\
        --subsystem execution --criticality high --source QE-51 \\
        --failure-mode "..." --status planned --planned-in PR-013

    qe_new_requirement.py ... --status verified \\
        --test tests/execution/test_thing.py --test-type regression

`--dry-run` prints the entry without writing, which is the mode to use when
showing somebody what would be added.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qe_registry_refs import GitUnavailable, ids_on_refs  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO = SKILL_DIR.parent.parent.parent
REGISTRY_PATH = REPO / "config" / "quality_registry.json"
SCHEMA_PATH = REPO / "config" / "quality_registry.schema.json"

KIND_BY_PREFIX = {
    "INV-": "invariant",
    "REG-": "regression",
    "SEC-": "security_regression",
}

ID_RE = re.compile(r"^(INV-[0-9]{3}|REG-[0-9]{4}|SEC-[0-9]{4}|[A-Z]{3,5}-[0-9]{3})$")


def kind_for(entry_id: str) -> str:
    for prefix, kind in KIND_BY_PREFIX.items():
        if entry_id.startswith(prefix):
            return kind
    return "requirement"


def taken_ids(registry: dict[str, Any], *, local_only: bool = False) -> dict[str, str]:
    """
    Every id that is spoken for: in this working tree, or on any other ref.

    Scanning the working tree alone sees only what has merged to main, so two
    branches open at once are handed the same next id. That happened three
    times in one session and was caught by hand every time; nothing failed.
    """
    taken = {entry["id"]: "the working tree" for entry in registry["entries"]}
    if local_only:
        return taken
    try:
        for entry_id, ref in ids_on_refs(REPO).items():
            taken.setdefault(entry_id, ref)
    except GitUnavailable as exc:
        # Fail open: no git is a worse reason to refuse than a stale scan.
        print(f"[warn] could not read other refs ({exc}); ids may collide", file=sys.stderr)
    return taken


def next_id(taken: dict[str, str], prefix: str) -> str:
    """The next free id for a prefix, so nobody has to scan the file for one."""
    width = 4 if prefix in {"REG-", "SEC-"} else 3
    used = [
        int(entry_id[len(prefix) :])
        for entry_id in taken
        if entry_id.startswith(prefix) and entry_id[len(prefix) :].isdigit()
    ]
    return f"{prefix}{max(used, default=0) + 1:0{width}d}"


def build_entry(args: argparse.Namespace) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": args.id,
        "kind": kind_for(args.id),
        "title": args.title,
        "statement": args.statement,
        "criticality": args.criticality,
        "subsystem": args.subsystem,
        "status": args.status,
        "source": args.source,
        "failure_mode": args.failure_mode,
    }
    if args.owning_module:
        entry["owning_modules"] = list(args.owning_module)
    if args.depends_on:
        entry["depends_on"] = list(args.depends_on)

    if args.status in {"verified", "partial"}:
        if not args.test:
            raise SystemExit(
                f"status {args.status!r} is a claim about evidence: pass --test "
                "(and --test-type) naming the test that decides it"
            )
        entry["verification"] = [{"test": test, "test_type": args.test_type} for test in args.test]
    elif args.test:
        raise SystemExit(
            f"status {args.status!r} means there is no evidence yet; drop --test "
            "or change the status"
        )

    if args.status in {"partial", "planned"}:
        if not args.planned_in:
            raise SystemExit(f"status {args.status!r} needs --planned-in PR-NNN")
        entry["planned_in"] = args.planned_in
    elif args.planned_in:
        raise SystemExit(f"status {args.status!r} must not name a phase")

    if args.note:
        entry["notes"] = args.note
    return entry


def validate(registry: dict[str, Any]) -> list[str]:
    try:
        from jsonschema import Draft7Validator
    except ImportError:
        print("[warn] jsonschema not installed; wrote without validating", file=sys.stderr)
        return []
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft7Validator(schema)
    out = []
    for error in validator.iter_errors(registry):
        where = ".".join(str(p) for p in error.path) or "<root>"
        out.append(f"{where}: {error.message}")
    return out


def validate_with_loader(registry: dict[str, Any]) -> list[str]:
    """
    Run the *gate's* loader over the candidate registry, not just the schema.

    The schema does not know the test-type taxonomy, so the scaffolder once
    printed `[ok  ] added SEC-0001` for an entry `load_registry` then refused
    (`test_type="governance"` is not a declared type). A scaffolder that
    declares success for something the gate rejects is worse than no
    scaffolder: it moves the failure to CI and makes the entry look reviewed.
    """
    try:
        sys.path.insert(0, str(REPO))
        from src.quality.registry import RegistryError, load_registry
    except ImportError as exc:
        print(
            f"[warn] loader unavailable ({exc}); validated against the schema only", file=sys.stderr
        )
        return []

    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / REGISTRY_PATH.name
        candidate.write_text(
            json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        try:
            load_registry(path=candidate, root=REPO)
        except RegistryError as exc:
            return [str(exc)]
    return []


def insert_sorted(registry: dict[str, Any], entry: dict[str, Any]) -> None:
    """
    Keep entries grouped by prefix and ordered by number.

    Appending to the end works and makes the file progressively harder to
    read; the diff of an ordered insert is one hunk either way.
    """
    entries = registry["entries"]
    prefix = entry["id"].rsplit("-", 1)[0] + "-"
    position = len(entries)
    for index, existing in enumerate(entries):
        if existing["id"].startswith(prefix) and existing["id"] > entry["id"]:
            position = index
            break
    else:
        last = max((i for i, e in enumerate(entries) if e["id"].startswith(prefix)), default=None)
        if last is not None:
            position = last + 1
    entries.insert(position, entry)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="registry id; omit with --prefix to take the next free one")
    parser.add_argument("--prefix", help="allocate the next free id for this prefix, e.g. REG-")
    parser.add_argument("--title", required=True)
    parser.add_argument("--statement", required=True, help="a testable proposition, not a slogan")
    parser.add_argument(
        "--criticality", required=True, choices=["critical", "high", "medium", "low"]
    )
    parser.add_argument("--subsystem", required=True)
    parser.add_argument("--source", required=True, help="e.g. QE-42")
    parser.add_argument("--failure-mode", required=True, help="what goes wrong in production")
    parser.add_argument(
        "--status", required=True, choices=["verified", "partial", "planned", "accepted_gap"]
    )
    parser.add_argument("--planned-in", help="PR-NNN, for partial and planned")
    parser.add_argument("--test", action="append", help="repeatable; for verified and partial")
    parser.add_argument("--test-type", default="unit")
    parser.add_argument("--owning-module", action="append")
    parser.add_argument("--depends-on", action="append")
    parser.add_argument("--note")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="allocate against this working tree alone, ignoring other branches",
    )
    args = parser.parse_args(argv)

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    taken = taken_ids(registry, local_only=args.local_only)

    if not args.id:
        if not args.prefix:
            raise SystemExit("pass --id, or --prefix to allocate the next free one")
        args.id = next_id(taken, args.prefix)
    if not ID_RE.match(args.id):
        raise SystemExit(f"{args.id!r} is not a well-formed registry id")
    if args.id in taken:
        raise SystemExit(f"{args.id} already exists on {taken[args.id]}")
    if args.status == "accepted_gap":
        raise SystemExit(
            "an accepted gap needs a waiver with an expiry and an argument for why "
            "it is not critical; add it by hand so the argument lands in the diff"
        )

    entry = build_entry(args)
    insert_sorted(registry, entry)

    problems = validate(registry) or validate_with_loader(registry)
    if problems:
        print("[FAIL] the resulting registry does not validate:")
        for problem in problems[:5]:
            print(f"         {problem}")
        return 1

    if args.dry_run:
        print(json.dumps(entry, indent=2))
        print("\n(dry run: nothing written)")
        return 0

    REGISTRY_PATH.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"[ok  ] added {entry['id']} ({entry['kind']}, {entry['status']})")
    print("       next: write the test, then regenerate the traceability document:")
    print("       python3 scripts/generate_quality_docs.py")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    sys.exit(main())
