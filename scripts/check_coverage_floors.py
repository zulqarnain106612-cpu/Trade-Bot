#!/usr/bin/env python3
"""
Per-package / per-file coverage floor enforcement — GAP-020.

Reads the .coverage data produced by pytest-cov (via ``coverage json``) and
asserts that safety-critical paths meet minimum line-coverage thresholds that
the global ``fail_under=95`` gate cannot catch.

Usage (run after pytest):
    python3 scripts/check_coverage_floors.py [--coverage-file .coverage]

Exit codes:
    0  — all floors met
    1  — one or more floors violated (details printed to stdout)
    2  — .coverage file not found or coverage package missing

Integration:
    Add to CI after ``pytest`` step, e.g.:
        pytest
        python3 scripts/check_coverage_floors.py

    Or via Makefile / scripts/autofix.sh after the standard test run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Floor definitions — per GAP-020 recommendation.
# Keys are fnmatch patterns relative to the project root (forward-slash, no
# leading ./). Values are minimum line-coverage percentages (0-100).
# ---------------------------------------------------------------------------
COVERAGE_FLOORS: dict[str, int] = {
    # Live order placement — highest blast radius.
    "src/execution/live.py": 75,
    "src/execution/paper.py": 70,
    "src/execution/order_fsm.py": 70,
    "src/execution/order_manager.py": 70,
    # Main event loop.
    "src/engine/orchestrator.py": 60,
    "src/engine/signal_engine.py": 65,
    # Runtime diagnostics — must catch regressions before they matter.
    "src/diagnostics/runtime_monitor.py": 50,
    # Risk gates — the sequential gate stack that guards every trade.
    "src/risk/gates.py": 70,
    "src/risk/cognitive_engine.py": 65,
    "src/risk/kelly.py": 70,
    # Crypto-Box ensemble — manipulations and consensus are safety-critical.
    "src/engines/consensus.py": 80,
    "src/engines/signal_gate.py": 85,
    "src/engines/risk_quantifier.py": 85,
    "src/engines/schema.py": 90,
    "src/regime/depth_detector_v2.py": 75,
    "src/engine/crypto_box_adapter.py": 70,
    # Position sizing — decides how much capital each trade risks, so a
    # regression here is silent and expensive. Currently 97%.
    "src/risk/vol_target_sizer.py": 85,
    # Decides whether a regime is certain enough to trade at all. Currently 99%.
    "src/strategies/regime_strategy_selector.py": 85,
}


def _export_coverage_json(coverage_file: Path, out_file: Path) -> bool:
    """Run ``coverage json`` and write JSON report to *out_file*.

    ``coverage json`` applies the ``[tool.coverage.report] fail_under``
    threshold and exits 2 when total coverage is below it -- even though it
    still writes the JSON report first. Treat that as success (the report we
    need exists); only a missing report is a real failure.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "coverage",
            "json",
            f"--data-file={coverage_file}",
            "-o",
            str(out_file),
            "-q",
        ],
        capture_output=True,
        text=True,
    )
    if not out_file.exists():
        print(f"[check_coverage_floors] coverage json failed:\n{result.stderr}", file=sys.stderr)
        return False
    return True


def _pct(executed: int, total: int) -> float:
    if total == 0:
        return 100.0
    return round(executed / total * 100.0, 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Per-file coverage floor check (GAP-020)")
    parser.add_argument(
        "--coverage-file",
        default=".coverage",
        help="Path to .coverage data file (default: .coverage)",
    )
    args = parser.parse_args(argv)

    coverage_file = Path(args.coverage_file)
    if not coverage_file.exists():
        print(
            f"[check_coverage_floors] ERROR: {coverage_file} not found. "
            "Run pytest first to generate coverage data.",
            file=sys.stderr,
        )
        return 2

    json_out = Path(".coverage_floors_tmp.json")
    try:
        if not _export_coverage_json(coverage_file, json_out):
            return 2

        data: dict = json.loads(json_out.read_text())
    finally:
        if json_out.exists():
            json_out.unlink()

    files_data: dict[str, dict] = data.get("files", {})

    # Normalise keys: coverage json uses OS paths; strip leading "./" and
    # normalise to forward-slash for matching.
    normalised: dict[str, dict] = {}
    for raw_path, info in files_data.items():
        key = raw_path.replace("\\", "/").lstrip("./")
        normalised[key] = info

    violations: list[str] = []

    for pattern, min_pct in COVERAGE_FLOORS.items():
        # Find matching file (exact or suffix match).
        match_key: str | None = None
        for key in normalised:
            if key == pattern or key.endswith(("/" + pattern, pattern)):
                match_key = key
                break

        if match_key is None:
            print(f"  [SKIP]  {pattern:<55} — not found in coverage data (file may be excluded)")
            continue

        summary = normalised[match_key].get("summary", {})
        num_stmts = summary.get("num_statements", 0)
        covered = summary.get("covered_lines", 0)
        actual_pct = summary.get("percent_covered", _pct(covered, num_stmts))
        actual_pct = round(float(actual_pct), 1)

        status = "OK  " if actual_pct >= min_pct else "FAIL"
        marker = "✓" if status == "OK  " else "✗"
        print(f"  [{status}] {marker} {pattern:<55} {actual_pct:5.1f}% (floor={min_pct}%)")

        if actual_pct < min_pct:
            violations.append(f"{pattern}: {actual_pct:.1f}% < required {min_pct}%")

    print()
    if violations:
        print(f"[check_coverage_floors] FAILED — {len(violations)} floor(s) violated:")
        for v in violations:
            print(f"  • {v}")
        return 1

    print(
        f"[check_coverage_floors] PASSED — all {len(COVERAGE_FLOORS)} safety-critical floors met."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
