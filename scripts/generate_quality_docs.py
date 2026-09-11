#!/usr/bin/env python3
"""
Render docs/quality/REQUIREMENTS_TRACEABILITY.md from config/quality_registry.json.

The traceability matrix is the document an auditor and an on-call operator both
read, and a hand-maintained matrix is exactly the kind of file that says a
requirement is covered by a test someone deleted last quarter. So it is
generated, and the registry behind it is validated on load: a named test that is
not on disk fails the build rather than the matrix.

Usage:
    python3 scripts/generate_quality_docs.py            # write the document
    python3 scripts/generate_quality_docs.py --check    # fail if it is stale

--check is what CI runs. It exits 1 when the file on disk differs from what the
registry would produce, which catches someone editing the prose instead of the
data.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Anchor the repo root on __file__ inside the insert itself. Running this as
# `python scripts/generate_quality_docs.py` puts scripts/ on sys.path, never the
# repo root, so the first-party import below would fail from anywhere but the
# root. tests/test_scripts_path_bootstrap.py enforces this shape.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from src.quality.registry import QualityRegistry, RegistryEntry, load_registry  # noqa: E402

OUTPUT = PROJECT_ROOT / "docs" / "quality" / "REQUIREMENTS_TRACEABILITY.md"

SUBSYSTEM_TITLES = {
    "risk": "Risk",
    "execution": "Execution",
    "portfolio": "Portfolio",
    "signal": "Signal and features",
    "model": "Models and leakage",
    "data": "Data, money and time",
    "api": "API and WebSocket",
    "security": "Cryptography and secrets",
    "supply_chain": "Supply chain and artifacts",
    "resilience": "Resilience and recovery",
    "release": "Release and production",
    "governance": "Governance",
}

KIND_ORDER = ["invariant", "requirement", "regression", "security_regression"]

STATUS_LABELS = {
    "verified": "VERIFIED",
    "partial": "PARTIAL",
    "planned": "PLANNED",
    "accepted_gap": "ACCEPTED GAP",
}

PREAMBLE = """<!--
GENERATED FILE — DO NOT EDIT BY HAND.

Source of truth: config/quality_registry.json
Regenerate with: python3 scripts/generate_quality_docs.py
CI check:        python3 scripts/generate_quality_docs.py --check

Editing this file directly will be reverted by the next regeneration and will
fail CI. Change the registry instead; the registry is reviewed as a diff and
validated by src/quality/registry.py at load time.
-->

# Requirements traceability

Every requirement, trading invariant, regression and security regression this
project holds itself to, and the test that decides each one.

The chain this document exists to make auditable:

```
Requirement
   ↓
Implementation
   ↓
Test
   ↓
CI gate
   ↓
Release evidence
```

A row here is a claim about the tree, not an aspiration. The loader stats every
named test file and every named owning module, so a row cannot survive the
deletion of the thing it points at.

## How to read a status

"""

FOOTER_TEMPLATE = """
---

## Governance

This document is generated from `config/quality_registry.json`, which is
validated on load by `src/quality/registry.py` and in CI by
`tests/quality/test_quality_registry.py`. The validation is not decorative:

- An entry marked `verified` or `partial` must name at least one test, and
  every named test **must exist on disk**. The registry cannot claim a test
  that was never written or that has since been deleted.
- An entry marked `planned` must name **no** test and **must** name the PR that
  will verify it. Unfinished work is not allowed to sit without an owner.
- An entry marked `accepted_gap` must carry a waiver with a reason, a person
  and a review date. A waiver with no expiry is a permanent hole.
- A `critical` entry can **never** be an `accepted_gap`. To waive it you must
  first argue, in the diff, that it is not critical.
- The id prefix and the declared `kind` are one fact: `INV-` is an invariant,
  `REG-` a regression, `SEC-` a security regression, anything else a
  requirement. A mistyped kind cannot hide an invariant among the requirements.
- Every `depends_on` must resolve, and the dependency graph must be acyclic.

To add or change an entry, edit the registry and regenerate this file. See
`docs/quality/QUALITY_POLICY.md` for the policy these requirements serve,
`docs/quality/TEST_STRATEGY.md` for the taxonomy the `test_type` column draws
on, and `docs/quality/IMPLEMENTATION_PLAN.md` for what each phase delivers.

Registry version: {version} — {count} entries.
"""


def _status_cell(entry: RegistryEntry) -> str:
    label = STATUS_LABELS[entry.status]
    if entry.planned_in:
        return f"{label} → {entry.planned_in}"
    return label


def render_entry(entry: RegistryEntry) -> str:
    """Render one registry entry as a documentation block."""
    lines = [f"#### `{entry.id}` — {entry.title}\n\n"]
    lines.append(
        f"**{_status_cell(entry)}** · {entry.criticality} · {entry.kind} · "
        f"source: {entry.source}\n\n"
    )
    lines.append(f"{entry.statement}\n\n")

    if entry.failure_mode:
        lines.append(f"- **If violated:** {entry.failure_mode}\n")
    if entry.owning_modules:
        modules = ", ".join(f"`{m}`" for m in entry.owning_modules)
        lines.append(f"- **Owned by:** {modules}\n")
    if entry.depends_on:
        deps = ", ".join(f"`{d}`" for d in entry.depends_on)
        lines.append(f"- **Depends on:** {deps}\n")

    if entry.verification:
        lines.append("- **Verification:**\n")
        for ver in entry.verification:
            note = f" — {ver.note}" if ver.note else ""
            lines.append(f"  - `{ver.test}` ({ver.test_type}){note}\n")
    else:
        lines.append("- **Verification:** none yet\n")

    if entry.waiver:
        lines.append(
            f"- **Waiver:** {entry.waiver.reason} "
            f"(accepted by {entry.waiver.accepted_by}, review by {entry.waiver.review_by})\n"
        )
    if entry.notes:
        lines.append(f"\n> {entry.notes}\n")

    lines.append("\n")
    return "".join(lines)


def render(registry: QualityRegistry) -> str:
    """Render the whole document."""
    parts = [PREAMBLE]

    for status, label in STATUS_LABELS.items():
        parts.append(f"**{label}** — {registry.status_definitions[status]}\n\n")

    counts = registry.coverage_by_status()
    parts.append("## Summary by status\n\n")
    parts.append("| Status | Entries |\n|---|---|\n")
    for status, label in STATUS_LABELS.items():
        parts.append(f"| {label} | {counts[status]} |\n")
    parts.append(f"| **Total** | **{len(registry)}** |\n\n")

    parts.append("## Summary by subsystem\n\n")
    parts.append("| Subsystem | Entries | Verified |\n|---|---|---|\n")
    for subsystem in registry.subsystems:
        entries = registry.by_subsystem(subsystem)
        verified = sum(1 for e in entries if e.is_verified)
        parts.append(f"| {SUBSYSTEM_TITLES[subsystem]} | {len(entries)} | {verified} |\n")
    parts.append("\n")

    parts.append("## Outstanding work by phase\n\n")
    parts.append("| Phase | Title | Entries |\n|---|---|---|\n")
    for phase, title in registry.phases.items():
        ids = [e.id for e in registry.by_phase(phase)]
        listed = ", ".join(f"`{i}`" for i in sorted(ids)) if ids else "—"
        parts.append(f"| {phase} | {title} | {listed} |\n")
    parts.append("\n---\n\n")

    for subsystem in registry.subsystems:
        entries = registry.by_subsystem(subsystem)
        if not entries:
            continue
        parts.append(f"## {SUBSYSTEM_TITLES[subsystem]}\n\n")
        ordered = sorted(
            entries,
            key=lambda e: (KIND_ORDER.index(e.kind), e.id),
        )
        for entry in ordered:
            parts.append(render_entry(entry))

    parts.append(FOOTER_TEMPLATE.format(version=registry.registry_version, count=len(registry)))
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the file on disk is stale instead of rewriting it",
    )
    args = parser.parse_args()

    registry = load_registry()
    rendered = render(registry)

    if args.check:
        if not OUTPUT.exists():
            print(f"{OUTPUT.relative_to(PROJECT_ROOT)} is missing; run without --check")
            return 1
        current = OUTPUT.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"{OUTPUT.relative_to(PROJECT_ROOT)} is stale. "
                f"Run: python3 scripts/generate_quality_docs.py"
            )
            return 1
        print(f"{OUTPUT.relative_to(PROJECT_ROOT)} is up to date.")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(PROJECT_ROOT)}: {len(registry)} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
