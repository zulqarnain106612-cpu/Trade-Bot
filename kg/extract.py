"""Extract stage: ONE cheap-model call per document, schema-constrained --
matches the cookbook's design ("haiku pulls entities and triples, one call
per doc") and bounds cost regardless of document length."""

from common.claude_cli import run_claude

from .config import KG_EXTRACT_MODEL
from .schema import EXTRACTION_SCHEMA


def extract_triples(text: str, source: str) -> list[dict]:
    prompt = (
        "Extract factual subject-predicate-object triples from this document. "
        "Use concise, canonical-looking entity names, not pronouns.\n\n"
        f"DOCUMENT:\n{text}"
    )
    out = run_claude(prompt, model=KG_EXTRACT_MODEL, json_schema=EXTRACTION_SCHEMA, max_turns=1)
    triples = (out.get("structured_output") or {}).get("triples", [])
    for t in triples:
        t["source"] = source
    return triples
