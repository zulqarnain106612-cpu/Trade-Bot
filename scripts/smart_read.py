#!/usr/bin/env python3
"""Compatibility wrapper for the compact file reader."""

import runpy
import sys
from pathlib import Path


root = Path(__file__).resolve().parent.parent
script = root / ".project-intel" / "scripts" / "smart_read.py"
if not script.exists():
    raise SystemExit(f"Missing smart read script: {script}")

sys.argv[0] = str(script)
runpy.run_path(str(script), run_name="__main__")
