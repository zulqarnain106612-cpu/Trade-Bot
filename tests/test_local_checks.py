"""REG-0024 — local execution is disabled and the old comment route is retired."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "local_checks.py"
DENIAL = (
    "THAPPAR — CI DIAGNOSTIC ACCESS BLOCKED. Use only: "
    "./scripts/prfail --pr <PR_NUMBER>. "
    "Direct or alternative diagnostic retrieval is prohibited."
)


def load_module():
    spec = importlib.util.spec_from_file_location("local_checks", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_prepare_function_is_retired() -> None:
    module = load_module()
    try:
        module.prepare()
    except SystemExit as exc:
        assert str(exc) == DENIAL
    else:
        raise AssertionError("legacy prepare() must not access GitHub or print the old notice")


def test_legacy_prepare_cli_is_denied() -> None:
    module = load_module()
    try:
        module.main(["prepare"])
    except SystemExit as exc:
        assert str(exc) == DENIAL
    else:
        raise AssertionError("legacy prepare CLI must be denied")


def test_local_run_cli_stays_disabled() -> None:
    module = load_module()
    try:
        module.main(["run"])
    except SystemExit as exc:
        assert "GitHub Actions is the sole test/build/check execution path" in str(exc)
    else:
        raise AssertionError("local check execution must remain disabled")


def test_policy_has_one_execution_path_and_retires_comment_subscription() -> None:
    from common.command_schema import LOCAL_CHECK_PATTERNS

    policy = json.loads((ROOT / "config" / "command_policy.json").read_text(encoding="utf-8"))
    local = policy["local_checks"]
    assert local["github_only"] is True
    assert local["run_command"].startswith("DISABLED:")
    assert tuple(local["blocked_patterns"]) == LOCAL_CHECK_PATTERNS
    assert local["prepare_command"] == "DISABLED: superseded by ./scripts/prfail --pr <PR_NUMBER>"

