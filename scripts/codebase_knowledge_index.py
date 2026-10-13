#!/usr/bin/env python3
"""Validate and refresh the source-backed codebase knowledge indexes.

The knowledge pack is a curated, partly user-owned artifact. This tool never
rewrites prose, existing priority classifications, diagram source, or semantic
anchor ranges. It deterministically rebuilds FILE_INDEX.md tracked-path rows,
metadata, and totals while preserving existing notes; newly tracked paths get
a conservative default priority/type. CHUNK_INDEX anchors are verified against
complete source-file hashes and current line bounds, and API_REFERENCE route rows
are compared with AST-derived method/path/handler/line tuples. A source edit
requires deliberate anchor review rather than silently re-labeling stale ranges.

Usage:
    python3 scripts/codebase_knowledge_index.py --check
    python3 scripts/codebase_knowledge_index.py --refresh-file-index
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE_DIR = PROJECT_ROOT / "codebase-analysis-docs"
FILE_INDEX = KNOWLEDGE_DIR / "FILE_INDEX.md"
CHUNK_INDEX = KNOWLEDGE_DIR / "CHUNK_INDEX.md"
MASTER = KNOWLEDGE_DIR / "CODEBASE_KNOWLEDGE.md"

# Index table rows: number, priority, path, type, line count, SHA8, notes.
FILE_ROW = re.compile(
    r"^(\|\s*\d+\s*\|\s*[^|]+\|\s*)([^|]+?)(\s*\|\s*[^|]+\|\s*)"
    r"(\d+|—|-)(\s*\|\s*)([0-9a-f]{8})(\s*\|.*)$"
)
CHUNK_META = re.compile(r"^(SHA8 )([0-9a-f]{8})(; total lines )(\d+)(\.)$")
ANCHOR = re.compile(r"\[\[F:([^#\]]+)#(\d+)-(\d+)#([0-9a-f]{8})\]\]")


def file_facts(path: Path) -> tuple[str, int]:
    """Return SHA-256 prefix and logical line count for a text file."""
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest()[:8], len(data.splitlines())


def tracked_paths(root: Path = PROJECT_ROOT) -> set[str]:
    """Read the authoritative tracked-path inventory without changing Git state."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "git ls-files failed: " + result.stderr.decode("utf-8", errors="replace").strip()
        )
    # The knowledge pack is intentionally excluded even when its files are
    # committed: FILE_INDEX cannot contain a stable self-hash, and these
    # artifacts describe the repository rather than being application inputs.
    return {
        path
        for item in result.stdout.split(b"\0")
        if item
        for path in (item.decode("utf-8", errors="surrogateescape"),)
        if not path.startswith("codebase-analysis-docs/")
    }


def _file_type(path_text: str) -> str:
    """Choose a conservative display type for newly tracked paths."""
    suffix = Path(path_text).suffix.lower()
    return {
        ".py": "Python",
        ".md": "Markdown",
        ".json": "JSON",
        ".yml": "YAML",
        ".yaml": "YAML",
        ".sh": "Shell",
        ".toml": "TOML",
        ".txt": "Text",
        ".js": "JavaScript",
        ".jsx": "React JSX",
        ".ts": "TypeScript",
        ".tsx": "TypeScript JSX",
        ".css": "CSS",
        ".html": "HTML",
    }.get(suffix, "Other text/source")


def _default_priority(path_text: str) -> str:
    """Assign a conservative default priority to newly tracked files."""
    if path_text.startswith(("scripts/", "tests/", "src/", "config/", ".github/")):
        return "P1"
    return "P2"


def refresh_file_index(text: str, root: Path = PROJECT_ROOT) -> tuple[str, list[str]]:
    """Rebuild tracked inventory metadata while preserving existing notes/priorities."""
    issues: list[str] = []
    lines = text.splitlines()
    try:
        expected = tracked_paths(root)
    except RuntimeError as exc:
        return text, [str(exc)]

    complete_marker = "## Complete tracked-file index"
    try:
        section_start = lines.index(complete_marker)
    except ValueError:
        return text, ["FILE_INDEX.md is missing the Complete tracked-file index section"]

    table_header_index = next(
        (i for i in range(section_start + 1, len(lines)) if lines[i].startswith("| ---")),
        None,
    )
    if table_header_index is None:
        return text, ["FILE_INDEX.md complete inventory has no table header"]

    existing: dict[str, tuple[str, str, str]] = {}
    for line in lines[table_header_index + 1 :]:
        match = FILE_ROW.match(line)
        if not match:
            continue
        path_text = match.group(2).strip()
        # Existing priorities, type labels, and human notes remain authoritative.
        fields = [part.strip() for part in line.strip().strip("|").split("|", 6)]
        if len(fields) >= 7:
            existing[path_text] = (fields[1], fields[3], fields[6])

    rows: list[tuple[str, str, str, str, str, str]] = []
    for path_text in sorted(expected):
        path = root / path_text
        if not path.is_file():
            issues.append(f"tracked path is missing from the working tree: {path_text}")
            continue
        digest, count = file_facts(path)
        priority, type_name, note = existing.get(
            path_text, (_default_priority(path_text), _file_type(path_text), "")
        )
        data = path.read_bytes()
        line_count = "—" if b"\0" in data[:8192] else str(count)
        rows.append((priority, path_text, type_name, line_count, digest, note))

    if issues:
        return text, issues

    priority_order = {"P0": 0, "P1": 1, "P2": 2}
    rows.sort(key=lambda row: (priority_order.get(row[0], 3), row[1]))
    rendered_rows = [
        f"| {i} | {priority} | {path_text} | {type_name} | {line_count} | {digest} | {note} |"
        for i, (priority, path_text, type_name, line_count, digest, note) in enumerate(rows, 1)
    ]

    # Refresh metadata in the small reference table without changing its notes.
    refreshed_top: list[str] = []
    for line in lines[:section_start]:
        match = FILE_ROW.match(line)
        if not match:
            refreshed_top.append(line)
            continue
        path_text = match.group(2).strip()
        path = root / path_text
        if not path.is_file():
            issues.append(f"reference path is missing from the working tree: {path_text}")
            refreshed_top.append(line)
            continue
        digest, count = file_facts(path)
        old_count = match.group(4)
        count_text = str(count) if old_count not in {"—", "-"} else old_count
        refreshed_top.append(
            match.group(1)
            + path_text
            + match.group(3)
            + count_text
            + match.group(5)
            + digest
            + match.group(7)
        )

    if issues:
        return text, issues

    header = lines[section_start : table_header_index + 1]
    result_lines = refreshed_top + header + rendered_rows
    totals = {
        "Tracked paths": len(rows),
        "Python files": sum(1 for _, path_text, *_ in rows if path_text.endswith(".py")),
        "P0 anchors": sum(1 for priority, *_ in rows if priority == "P0"),
        "P1 files": sum(1 for priority, *_ in rows if priority == "P1"),
    }
    result = "\n".join(result_lines) + ("\n" if text.endswith("\n") else "")
    for label, value in totals.items():
        result = re.sub(
            rf"^- {re.escape(label)}: \d+$",
            f"- {label}: {value}",
            result,
            count=1,
            flags=re.MULTILINE,
        )
    return result, issues


def validate_anchors(text: str, root: Path = PROJECT_ROOT) -> list[str]:
    """Verify chunk metadata, stable anchors, file existence and range bounds."""
    issues: list[str] = []
    chunk_text = (root / "codebase-analysis-docs/CHUNK_INDEX.md").read_text(
        encoding="utf-8"
    )
    current_path: str | None = None

    for line in chunk_text.splitlines():
        heading = re.match(r"^###? (.+)$", line)
        if heading:
            candidate = heading.group(1).strip()
            current_path = candidate if (root / candidate).is_file() else None
            continue
        meta = CHUNK_META.match(line)
        if not meta or current_path is None:
            continue
        path = root / current_path
        digest, count = file_facts(path)
        if digest != meta.group(2) or count != int(meta.group(4)):
            issues.append(
                f"stale CHUNK_INDEX metadata for {current_path}: "
                f"indexed={meta.group(2)}/{meta.group(4)} actual={digest}/{count}"
            )

    for index_name, index_text in (
        ("CHUNK_INDEX.md", chunk_text),
        ("CODEBASE_KNOWLEDGE.md", text),
    ):
        for path_text, start_text, end_text, digest in ANCHOR.findall(index_text):
            path = root / path_text
            if not path.is_file():
                issues.append(f"{index_name} anchor references missing path: {path_text}")
                continue
            actual_digest, line_count = file_facts(path)
            start, end = int(start_text), int(end_text)
            if digest != actual_digest:
                issues.append(
                    f"stale {index_name} anchor hash for {path_text}#{start}-{end}: "
                    f"indexed={digest} actual={actual_digest}"
                )
            if start < 1 or end < start or end > line_count:
                issues.append(
                    f"invalid {index_name} anchor range for {path_text}#{start}-{end}; "
                    f"current lines={line_count}"
                )
    return issues


def validate_api_reference(source_text: str, reference_text: str) -> list[str]:
    """Compare the API route inventory with AST-derived method/path/handler/line tuples."""
    methods = {"get", "post", "put", "patch", "delete", "options", "head", "websocket"}
    tree = ast.parse(source_text)
    source_routes: list[tuple[str, str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if (
                not isinstance(decorator.func.value, ast.Name)
                or decorator.func.value.id != "app"
                or decorator.func.attr not in methods
                or not decorator.args
                or not isinstance(decorator.args[0], ast.Constant)
                or not isinstance(decorator.args[0].value, str)
            ):
                continue
            method = (
                "WEBSOCKET"
                if decorator.func.attr == "websocket"
                else decorator.func.attr.upper()
            )
            source_routes.append((method, decorator.args[0].value, node.name, node.lineno))

    all_routes_marker = "## All routes"
    if all_routes_marker not in reference_text:
        return ["API_REFERENCE.md is missing the All routes section"]
    route_section = reference_text.split(all_routes_marker, 1)[1]
    row_pattern = re.compile(
        r"^\|\s*([A-Z]+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|"
    )
    documented_routes = [
        (match.group(1), match.group(2).strip(), match.group(3).strip(), int(match.group(4)))
        for line in route_section.splitlines()
        if (match := row_pattern.match(line))
    ]
    source_routes.sort()
    documented_routes.sort()
    if source_routes == documented_routes:
        return []
    missing = sorted(set(source_routes) - set(documented_routes))
    stale = sorted(set(documented_routes) - set(source_routes))
    details = []
    if missing:
        details.append(f"missing source routes: {missing[:4]}")
    if stale:
        details.append(f"stale/extra documented routes: {stale[:4]}")
    return [
        f"API_REFERENCE.md route inventory differs: source={len(source_routes)}, "
        f"documented={len(documented_routes)}; " + "; ".join(details)
    ]


def check(root: Path = PROJECT_ROOT) -> list[str]:
    """Return all structural integrity failures without modifying files."""
    issues: list[str] = []
    required_paths = (
        root / "codebase-analysis-docs/FILE_INDEX.md",
        root / "codebase-analysis-docs/CHUNK_INDEX.md",
        root / "codebase-analysis-docs/CODEBASE_KNOWLEDGE.md",
        root / "codebase-analysis-docs/API_REFERENCE.md",
    )
    for path in required_paths:
        if not path.is_file():
            issues.append(f"required knowledge artifact is missing: {path.relative_to(root)}")
    if issues:
        return issues

    index_path = required_paths[0]
    master_path = required_paths[2]
    original = index_path.read_text(encoding="utf-8")
    refreshed, index_issues = refresh_file_index(original, root)
    issues.extend(index_issues)
    if refreshed != original:
        issues.append("FILE_INDEX.md metadata is stale; run --refresh-file-index")
    master = master_path.read_text(encoding="utf-8")
    issues.extend(validate_anchors(master, root))
    api_source = (root / "src/api/main.py").read_text(encoding="utf-8")
    api_reference = (root / "codebase-analysis-docs/API_REFERENCE.md").read_text(
        encoding="utf-8"
    )
    issues.extend(validate_api_reference(api_source, api_reference))
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--check", action="store_true", help="validate index integrity without writes"
    )
    modes.add_argument(
        "--refresh-file-index",
        action="store_true",
        help=(
            "rebuild FILE_INDEX rows/totals while preserving notes; "
            "never rewrite semantic anchors"
        ),
    )
    args = parser.parse_args(argv)

    if args.refresh_file_index:
        path = PROJECT_ROOT / FILE_INDEX.relative_to(PROJECT_ROOT)
        original = path.read_text(encoding="utf-8")
        refreshed, issues = refresh_file_index(original)
        if issues:
            print("REFUSED: " + "; ".join(issues), file=sys.stderr)
            return 2
        path.write_text(refreshed, encoding="utf-8")
        print("FILE_INDEX.md metadata refreshed; semantic notes and anchor ranges unchanged.")
        return 0

    issues = check()
    if issues:
        for issue in issues[:30]:
            print(f"STALE: {issue}", file=sys.stderr)
        if len(issues) > 30:
            print(f"... and {len(issues) - 30} more issues", file=sys.stderr)
        return 1
    print("Codebase knowledge indexes are structurally current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
