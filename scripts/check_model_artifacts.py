#!/usr/bin/env python3
"""
Model-artifact preflight.

Reads ``StorageSettings.model_dir`` and reports, per active timeframe, whether
a model bundle looks present on disk. The orchestrator's startup path will
train initial models automatically if none are present, but training on a
fresh checkout can take a very long time — this script gives an operator a
fast "is anything there?" answer before starting the loop.

Exit codes:
    0  every active timeframe has at least one artifact file under model_dir
    1  one or more timeframes have no artifact — training will run on boot
    2  bad configuration (model_dir missing / unreadable)

This is a heuristic. It does not verify manifest signatures — that runs
inside src/models/trainer.py's ``_verify_manifest`` on load.
"""

from __future__ import annotations

import sys
from pathlib import Path

from src.config import get_settings


def main() -> int:
    settings = get_settings()
    model_dir: Path = settings.storage.model_dir

    if not model_dir.exists():
        print(
            f"[FAIL] model_dir does not exist: {model_dir}\n"
            "       Startup will create it and train from scratch.",
            file=sys.stderr,
        )
        return 2

    if not model_dir.is_dir():
        print(f"[FAIL] model_dir is not a directory: {model_dir}", file=sys.stderr)
        return 2

    all_present = True
    for tf in settings.active_timeframes:
        # Trainer naming convention: files under model_dir contain the
        # timeframe value in their name (see src/models/trainer.py save
        # paths). A missing timeframe means startup will retrain it.
        matches = list(model_dir.rglob(f"*{tf.value}*"))
        matches = [p for p in matches if p.is_file()]
        if matches:
            print(f"[ OK ] {tf.value}: {len(matches)} file(s)")
        else:
            print(f"[MISS] {tf.value}: no artifacts under {model_dir}")
            all_present = False

    return 0 if all_present else 1


if __name__ == "__main__":
    sys.exit(main())
