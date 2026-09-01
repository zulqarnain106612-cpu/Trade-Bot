from .assemble import assemble
from .extract import extract_triples


def ingest_document(text: str, source: str) -> dict:
    triples = extract_triples(text, source)
    stats = assemble(triples)
    stats["triples_extracted"] = len(triples)
    return stats
