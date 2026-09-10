#!/usr/bin/env python3
"""
Render docs/MATH_FOUNDATIONS.md from config/math_registry.json.

The document is generated rather than written because a hand-maintained
companion to a data file drifts, and a drifted document is worse than none: it
is the thing people actually read. The registry is the source of truth; this
script is the only writer of the generated section.

Usage:
    python3 scripts/generate_math_docs.py            # write the document
    python3 scripts/generate_math_docs.py --check    # fail if it is stale

--check is what CI runs. It exits 1 when the file on disk differs from what the
registry would produce, which catches the case of someone editing the prose
instead of the data.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Anchor the repo root on __file__ inside the insert itself. Running this as
# `python scripts/generate_math_docs.py` puts scripts/ on sys.path, never the
# repo root, so the first-party import below would fail from anywhere but the
# root. tests/test_scripts_path_bootstrap.py enforces this shape.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from src.mathcore.registry import MathRegistry, RegistryEntry, load_registry  # noqa: E402

OUTPUT = PROJECT_ROOT / "docs" / "MATH_FOUNDATIONS.md"

DOMAIN_TITLES = {
    "algebra": "Algebra",
    "number_theory": "Number theory",
    "harmonic_analysis": "Fourier and harmonic analysis",
    "probability_information": "Probability and information theory",
    "coding_theory": "Coding theory",
    "geometry_graphs": "Geometry, lattices and graphs",
    "constants": "Constants",
    "protocol_primitives": "Protocol primitives",
    "numerology": "Numerology and folklore",
}

DOMAIN_ORDER = [
    "algebra",
    "number_theory",
    "harmonic_analysis",
    "probability_information",
    "coding_theory",
    "geometry_graphs",
    "constants",
    "protocol_primitives",
    "numerology",
]

VERDICT_LABELS = {
    "load_bearing": "LOAD-BEARING",
    "performance_critical": "PERFORMANCE-CRITICAL",
    "attack_surface": "ATTACK SURFACE",
    "provenance_only": "PROVENANCE ONLY",
    "folklore": "FOLKLORE",
}

PREAMBLE = """<!--
GENERATED FILE — DO NOT EDIT BY HAND.

Source of truth: config/math_registry.json
Regenerate with: python3 scripts/generate_math_docs.py
CI check:        python3 scripts/generate_math_docs.py --check

Editing this file directly will be reverted by the next regeneration and will
fail CI. Change the registry instead; the registry is reviewed as a diff and
validated by src/mathcore/registry.py at load time.
-->

# Mathematical foundations

Every mathematical object that has, or is claimed to have, a role in
cryptography or cryptocurrency, with an explicit verdict on whether it does.

The list exists because the question attracts two opposite errors. One is to
assume that anything with a Greek letter is doing serious work, which is how
the golden ratio ends up in a trading strategy. The other is to dismiss
constants as decoration, which is how a structural use of pi in lattice
cryptography gets mistaken for the same kind of thing as pi in Blowfish's
S-boxes. Both errors are avoided by naming, for each object, what breaks if it
is removed.

## How to read a verdict

The dividing line throughout: **an object is doing real work only if removing
it breaks a proof.** If it can be swapped for any other value or structure of
similar size with no security consequence, it is provenance at best. If it is
being used to predict prices, it is numerology.

"""

FOOTER_TEMPLATE = """
---

## Governance

This document is generated from `config/math_registry.json`, which is
validated on load by `src/mathcore/registry.py` and in CI by
`tests/test_math_registry.py`. The validation is not decorative:

- An entry marked `implemented` must name an owning module that **exists on
  disk**. The registry cannot claim code that was never written.
- An entry marked `folklore` may **never** be marked `implemented`. Numerology
  cannot become a live signal by editing one field.
- Every `depends_on` must resolve, and the dependency graph must be acyclic.
  Both of these were violated by the first draft of this registry and caught by
  the loader, which is the reason the checks exist.
- Every security-relevant entry must state its concrete failure mode.

To add or change an entry, edit the registry and regenerate this file. See
`docs/MATH_ARCHITECTURE.md` for where each object lands in the tree, and
`docs/MATH_ROADMAP.md` for the order in which they are built.

Registry version: {version} — {count} entries.
"""


def _bullet_list(label: str, items: tuple[str, ...]) -> str:
    if not items:
        return ""
    return f"- **{label}:** {', '.join(items)}\n"


def render_entry(entry: RegistryEntry) -> str:
    """Render one registry entry as a documentation block."""
    lines = [f"#### {entry.name}\n"]
    lines.append(f"`{entry.id}` — **{VERDICT_LABELS[entry.verdict]}**")
    if entry.repo_relevance != "none":
        lines.append(f" · relevance: {entry.repo_relevance}")
    lines.append(f" · status: {entry.status}\n\n")
    lines.append(f"{entry.role}\n\n")

    lines.append(_bullet_list("Used by", entry.used_by))

    if entry.risk_if_misused:
        lines.append(f"- **Risk if misused:** {entry.risk_if_misused}\n")

    if entry.depends_on:
        deps = ", ".join(f"`{d}`" for d in entry.depends_on)
        lines.append(f"- **Depends on:** {deps}\n")

    owners = tuple(w.module for w in entry.owners)
    consumers = tuple(w.module for w in entry.consumers)
    if owners:
        lines.append(f"- **Owned by:** {', '.join(f'`{m}`' for m in owners)}\n")
    if consumers:
        lines.append(f"- **Consumed by:** {', '.join(f'`{m}`' for m in consumers)}\n")

    lines.append(_bullet_list("References", entry.references))

    if entry.notes:
        lines.append(f"\n> {entry.notes}\n")

    lines.append("\n")
    return "".join(lines)


def render(registry: MathRegistry) -> str:
    """Render the whole document."""
    parts = [PREAMBLE]

    for verdict, label in VERDICT_LABELS.items():
        definition = registry.verdict_definitions[verdict]
        parts.append(f"**{label}** — {definition}\n\n")

    parts.append("## Summary by verdict\n\n")
    parts.append("| Verdict | Entries |\n|---|---|\n")
    for verdict, label in VERDICT_LABELS.items():
        parts.append(f"| {label} | {len(registry.by_verdict(verdict))} |\n")
    parts.append("\n")

    parts.append("## Summary by domain\n\n")
    parts.append("| Domain | Entries |\n|---|---|\n")
    for domain in DOMAIN_ORDER:
        parts.append(f"| {DOMAIN_TITLES[domain]} | {len(registry.by_domain(domain))} |\n")
    parts.append("\n---\n\n")

    for domain in DOMAIN_ORDER:
        entries = registry.by_domain(domain)
        if not entries:
            continue
        parts.append(f"## {DOMAIN_TITLES[domain]}\n\n")
        for entry in entries:
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
                f"Run: python3 scripts/generate_math_docs.py"
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
