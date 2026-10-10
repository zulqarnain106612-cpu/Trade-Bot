"""``python -m src.terminal`` -- the same entry point as ``tradebot-term``."""

from __future__ import annotations

import sys

from src.terminal.cli import main

if __name__ == "__main__":
    sys.exit(main())
