"""No entry in the arch baseline may outlive the finding it suppresses.

config/arch_baseline.json records findings that were reviewed and accepted.
Nothing removed an entry when the underlying code was fixed, so suppressions
accumulated for work that had already been done -- six of them for
idempotency on order submission, which src/execution/idempotency.py has
implemented for some time.

A stale suppression is worse than clutter at this severity. Those six were
CRITICAL, and an order path added later *without* a client order id would
have produced a finding that landed next to five identical-looking accepted
ones. Duplicate order submission is a double-fill.

This runs the validator's scanner directly rather than through
scripts/arch_gate.sh: the CLI asks the LAW13 checklist questions
interactively, and the answers change what it reports. scan_directory() is
the file-based half, which is where every baselined fingerprint comes from.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VALIDATOR = REPO / ".claude" / "skills" / "crypto-architect" / "scripts" / "validate_arch.py"
BASELINE = REPO / "config" / "arch_baseline.json"
SCAN_DIR = REPO / "src"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_arch", VALIDATOR)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        pytest.skip(f"validator not importable at {VALIDATOR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["validate_arch"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def current_fingerprints() -> set[str]:
    if not VALIDATOR.exists():
        pytest.skip("crypto-architect skill not installed")
    validator = _load_validator()
    findings = validator.scan_directory(SCAN_DIR)
    return {f.fingerprint for f in findings if not f.passed}


def _baseline_fingerprints() -> set[str]:
    return set(json.loads(BASELINE.read_text(encoding="utf-8"))["fingerprints"])


def test_every_baseline_entry_still_matches_a_real_finding(
    current_fingerprints: set[str],
) -> None:
    stale = sorted(_baseline_fingerprints() - current_fingerprints)

    assert not stale, (
        "These entries in config/arch_baseline.json suppress findings the "
        "validator no longer reports. The code was fixed and the suppression "
        "was left behind -- delete them:\n  " + "\n  ".join(stale)
    )


def test_the_baseline_is_not_empty(current_fingerprints: set[str]) -> None:
    """Guards the test above against passing for the wrong reason.

    If scan_directory ever returns nothing -- a moved src/, a validator whose
    API changed -- then every baseline entry looks stale and the assertion
    above turns into a confusing failure, or an empty baseline makes it
    vacuous. Fail here instead, where the cause is obvious.
    """
    assert current_fingerprints, "the validator reported no findings at all"
    assert _baseline_fingerprints(), "the baseline is empty"
