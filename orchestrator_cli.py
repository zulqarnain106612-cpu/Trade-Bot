#!/usr/bin/env python3
import json
import sys

from orchestrator.run import run


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: python orchestrator_cli.py "<task>"', file=sys.stderr)
        sys.exit(1)
    print(json.dumps(run(" ".join(sys.argv[1:])), indent=2))


if __name__ == "__main__":
    main()
