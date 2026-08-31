"""Assemble stage: upsert canonical nodes + typed edges with provenance into
MongoDB. Idempotent via upsert-on-unique-key -- re-ingesting the same
document does not duplicate nodes or edges, only adds a source reference."""

from .db import get_nodes_collection, get_edges_collection
from .resolve import resolve_entities


def assemble(triples: list[dict]) -> dict:
    names = [t["subject"] for t in triples] + [t["object"] for t in triples]
    canon_map = resolve_entities(names)

    nodes_col = get_nodes_collection()
    edges_col = get_edges_collection()

    n_nodes, n_edges = 0, 0
    for t in triples:
        subj = canon_map.get(t["subject"], t["subject"])
        obj = canon_map.get(t["object"], t["object"])

        for name, ntype in (
            (subj, t.get("subject_type", "")),
            (obj, t.get("object_type", "")),
        ):
            res = nodes_col.update_one(
                {"name": name},
                {"$setOnInsert": {"name": name, "type": ntype}},
                upsert=True,
            )
            if res.upserted_id:
                n_nodes += 1

        res = edges_col.update_one(
            {"subject": subj, "predicate": t["predicate"], "object": obj},
            {
                "$setOnInsert": {"subject": subj, "predicate": t["predicate"], "object": obj},
                "$addToSet": {"sources": t.get("source", "unknown")},
            },
            upsert=True,
        )
        if res.upserted_id:
            n_edges += 1

    return {"nodes_upserted": n_nodes, "edges_upserted": n_edges}
