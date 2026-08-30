from .extract import extract_triples
from .assemble import assemble


def ingest_document(text: str, source: str) -> dict:
    triples = extract_triples(text, source)
    stats = assemble(triples)
    stats["triples_extracted"] = len(triples)
    return stats
