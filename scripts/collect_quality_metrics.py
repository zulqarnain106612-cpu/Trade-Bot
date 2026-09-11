#!/usr/bin/env python3
"""
Collect the quality metrics of docs/quality/QUALITY_METRICS.md.

GOV-007. The document listed seventeen metrics and nothing produced any of
them, which is the "fully specified, zero callers" shape this programme keeps
finding. This script produces the ones the repository can answer for itself;
the rest are marked `unavailable` with the reason, because a metrics report
that silently omits what it could not measure reads as a clean bill of health.

The most important number is not any single metric. It is:

> How many defects escaped each verification layer?

That is derived from the registry's `REG-####` and `SEC-####` entries, each of
which names the layer that should have caught it. With none filed yet the
count is zero and the report says so explicitly, rather than omitting the
section and implying there is nothing to measure.

Usage:
    python3 scripts/collect_quality_metrics.py
    python3 scripts/collect_quality_metrics.py --report quality-metrics.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Anchor the repo root on __file__ inside the insert itself, so this runs from
# anywhere. tests/test_scripts_path_bootstrap.py enforces this shape.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from src.quality.registry import load_registry  # noqa: E402

UNAVAILABLE = "unavailable"


def _unavailable(reason: str) -> dict[str, Any]:
    return {"value": None, "status": UNAVAILABLE, "reason": reason}


def registry_metrics() -> dict[str, Any]:
    """Everything derivable from config/quality_registry.json."""
    registry = load_registry()
    counts = registry.coverage_by_status()
    regressions = registry.by_kind("regression")
    security = registry.by_kind("security_regression")

    return {
        "requirements_total": {"value": len(registry), "status": "ok"},
        "requirements_verified": {"value": counts["verified"], "status": "ok"},
        "requirements_outstanding": {"value": len(registry.outstanding()), "status": "ok"},
        "requirements_waived": {"value": counts["accepted_gap"], "status": "ok"},
        "regression_count": {
            "value": len(regressions),
            "status": "ok",
            "note": "REG-#### entries: distinct defects that reached a running system.",
        },
        "security_regression_count": {
            "value": len(security),
            "status": "ok",
            "note": "SEC-#### entries: security weaknesses that were once present.",
        },
        "critical_unverified": {
            "value": len([e for e in registry.by_criticality("critical") if not e.is_verified]),
            "status": "ok",
            "note": "A critical requirement with no test behind it.",
        },
    }


def coverage_metrics() -> dict[str, Any]:
    """Branch coverage, read from an existing .coverage if pytest has run."""
    if not (PROJECT_ROOT / ".coverage").exists():
        return {
            "branch_coverage_pct": _unavailable("no .coverage file -- run pytest first"),
            "measured_files": _unavailable("no .coverage file -- run pytest first"),
        }
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "coverage", "json", "-o", "-", "--quiet"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "branch_coverage_pct": _unavailable("coverage could not read its data file"),
            "measured_files": _unavailable("coverage could not read its data file"),
        }
    report = json.loads(proc.stdout)
    return {
        "branch_coverage_pct": {
            "value": round(report["totals"]["percent_covered"], 2),
            "status": "ok",
        },
        "measured_files": {"value": len(report["files"]), "status": "ok"},
    }


def suite_metrics() -> dict[str, Any]:
    """Shape of the test suite, by the taxonomy directories it uses."""
    tests = PROJECT_ROOT / "tests"
    by_kind = {
        path.name: len(list(path.rglob("test_*.py")))
        for path in sorted(tests.iterdir())
        if path.is_dir() and not path.name.startswith("__")
    }
    return {
        "test_files_total": {"value": len(list(tests.rglob("test_*.py"))), "status": "ok"},
        "test_files_by_taxonomy": {"value": by_kind, "status": "ok"},
    }


def escaped_defects() -> dict[str, Any]:
    """
    The metric the document calls the most important one.

    Each REG/SEC entry names, in its notes, the layer that should have caught
    it. With none filed the answer is an explicit zero rather than an omitted
    section -- "we have not measured this" and "this is zero" are different
    statements and only one of them is good news.
    """
    registry = load_registry()
    defects = [*registry.by_kind("regression"), *registry.by_kind("security_regression")]
    if not defects:
        return {
            "escaped_defects_by_layer": {
                "value": {},
                "status": "ok",
                "note": (
                    "No REG-#### or SEC-#### entries have been filed. This is an "
                    "explicit zero, not an unmeasured metric: the first defect or "
                    "finding raised through the process files one."
                ),
            }
        }
    by_layer: dict[str, int] = {}
    for entry in defects:
        layer = entry.notes.split("layer:", 1)[-1].strip() if "layer:" in entry.notes else "unknown"
        by_layer[layer] = by_layer.get(layer, 0) + 1
    return {"escaped_defects_by_layer": {"value": by_layer, "status": "ok"}}


def operational_metrics() -> dict[str, Any]:
    """
    The metrics that need a running system, marked honestly.

    Listing them as unavailable rather than omitting them is the point: a
    report that quietly drops what it could not measure reads as a clean bill
    of health.
    """
    pending = {
        "mean_time_to_detect_s": "requires incident records (PR-010)",
        "mean_time_to_recover_s": "requires incident records (PR-010)",
        "deployment_failure_rate": "requires a production pipeline (PR-012)",
        "rollback_rate": "requires a production pipeline (PR-012)",
        "recovery_test_success": "requires restore drills (PR-010)",
        "model_drift": "requires a live drift detector",
        "signal_drift": "requires a live signal history",
        "data_freshness_s": "requires a live feed",
        "order_reconciliation_failures": "requires a live ledger (PR-010)",
        "mutation_score": "produced by scripts/check_mutation_score.py in the nightly job",
        "dependency_vulnerabilities": "produced by the security workflow (PR-009)",
        "secret_incidents": "produced by GitHub secret scanning (PR-009)",
    }
    return {name: _unavailable(reason) for name, reason in pending.items()}


def collect() -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    metrics.update(registry_metrics())
    metrics.update(coverage_metrics())
    metrics.update(suite_metrics())
    metrics.update(escaped_defects())
    metrics.update(operational_metrics())
    return {
        "collected_at": datetime.now(UTC).isoformat(),
        "metrics": metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="write the metrics as JSON")
    args = parser.parse_args()

    report = collect()
    for name, metric in sorted(report["metrics"].items()):
        if metric["status"] == UNAVAILABLE:
            print(f"[----] {name:<32} unavailable: {metric['reason']}")
        else:
            print(f"[ ok ] {name:<32} {metric['value']}")

    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
