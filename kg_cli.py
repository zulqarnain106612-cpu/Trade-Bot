#!/usr/bin/env python3
import argparse
import json
import sys

from kg.ingest import ingest_document
from kg.query import query


def main() -> None:
    p = argparse.ArgumentParser(prog="kg_cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="Extract+resolve+assemble a text file into the graph")
    ing.add_argument("path")

    q = sub.add_parser("query", help="Ask a multi-hop question over the graph")
    q.add_argument("text")

    args = p.parse_args()
    if args.cmd == "ingest":
        with open(args.path, encoding="utf-8") as f:
            stats = ingest_document(f.read(), source=args.path)
        print(json.dumps(stats, indent=2))
    elif args.cmd == "query":
        print(json.dumps(query(args.text), indent=2))


if __name__ == "__main__":
    sys.exit(main())
