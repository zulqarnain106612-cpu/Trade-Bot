#!/usr/bin/env python3
import argparse
import json
import sys

from rag_mongo.db import ensure_fulltext_index, ensure_vector_index
from rag_mongo.ingest import ingest_text
from rag_mongo.pipeline import answer_query


def main() -> None:
    p = argparse.ArgumentParser(prog="rag_cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Create vector + full-text indexes (one-time)")

    ing = sub.add_parser("ingest", help="Ingest a text file")
    ing.add_argument("path")

    q = sub.add_parser("query", help="Ask a question")
    q.add_argument("text")

    args = p.parse_args()

    if args.cmd == "init":
        ensure_vector_index()
        ensure_fulltext_index()
        print("Vector + full-text indexes ready.")
    elif args.cmd == "ingest":
        with open(args.path, encoding="utf-8") as f:
            n = ingest_text(f.read(), source=args.path)
        print(f"Ingested {n} chunks from {args.path}")
    elif args.cmd == "query":
        result = answer_query(args.text)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    sys.exit(main())
