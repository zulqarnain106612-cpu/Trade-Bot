"""JSON Schemas passed to `claude -p --json-schema` for structured output.
Keeping extraction/resolution schema-constrained is what makes this cheap:
one deterministic-shape call per document, no free-form parsing."""

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "triples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "subject_type": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "object_type": {"type": "string"},
                },
                "required": ["subject", "predicate", "object"],
            },
        }
    },
    "required": ["triples"],
}

RESOLUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "canonical_name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["canonical_name", "aliases"],
            },
        }
    },
    "required": ["clusters"],
}
