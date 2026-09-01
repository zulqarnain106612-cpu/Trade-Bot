"""Resolve stage: ONE batched call clusters every newly-seen entity name at
once. Deliberately not one call per entity/pair -- that would turn a single
document's ingestion into dozens of coordinator-tier calls for no benefit;
the model can cluster an entire name list in one schema-constrained call."""

from common.claude_cli import run_claude

from .config import KG_RESOLVE_MODEL
from .schema import RESOLUTION_SCHEMA


def resolve_entities(names: list[str]) -> dict[str, str]:
    """Returns {alias_or_name: canonical_name} for every input name."""
    if not names:
        return {}
    unique_names = sorted(set(names))
    prompt = (
        "Cluster these entity name strings into groups referring to the same "
        "real-world entity (aliases, name variants, abbreviations). Pick one "
        "canonical_name per cluster.\n\nNAMES:\n" + "\n".join(unique_names)
    )
    out = run_claude(prompt, model=KG_RESOLVE_MODEL, json_schema=RESOLUTION_SCHEMA, max_turns=1)
    clusters = (out.get("structured_output") or {}).get("clusters", [])
    mapping: dict[str, str] = {}
    for c in clusters:
        canon = c["canonical_name"]
        mapping[canon] = canon
        for alias in c.get("aliases", []):
            mapping[alias] = canon
    return mapping
