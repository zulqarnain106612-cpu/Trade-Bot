#!/usr/bin/env python3
"""Cloud-only retrieval step for PR review (Component 5).

Designed to run INSIDE a GitHub Actions runner -- never locally, per this
project's rule that review/test for this component happens exclusively in
GitHub's cloud service. Produces a bounded context.json that the
claude-review job attaches to the review prompt, so review is grounded in
the project's RAG + knowledge-graph store, not just the raw diff.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess

from review.config import REVIEW_MAX_CONTEXT_ITEMS
from review.retrieval import graph, hybrid


def _diff_text(base: str, head: str) -> str:
    out = subprocess.run(
        ["git", "diff", f"{base}...{head}"], capture_output=True, text=True, check=True
    )
    return out.stdout


def _candidate_terms(diff: str) -> list[str]:
    # Bounded: cap term count so a huge diff can't blow up the retrieval query.
    names = sorted(set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", diff)))
    return names[:50]


def build_context(base: str, head: str) -> dict:
    diff = _diff_text(base, head)
    terms = _candidate_terms(diff)
    query_text = " ".join(terms[:20]) or "code change"

    related_docs = hybrid(query_text, top_k=REVIEW_MAX_CONTEXT_ITEMS)
    related_edges = graph(terms, limit=REVIEW_MAX_CONTEXT_ITEMS)

    return {
        "related_documents": [
            {"source": d.get("source"), "excerpt": d["text"][:300]} for d in related_docs
        ],
        "related_facts": [
            f"({e['subject']}) -[{e['predicate']}]-> ({e['object']})" for e in related_edges
        ],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    ctx = build_context(args.base, args.head)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(ctx, f, indent=2)
    print(
        f"Wrote {args.out}: {len(ctx['related_documents'])} docs, {len(ctx['related_facts'])} facts"
    )
