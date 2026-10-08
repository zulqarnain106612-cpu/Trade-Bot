"""GOV-057 / GOV-063 -- permanent agent reliability boundary."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRE = ROOT / ".claude" / "hooks" / "pre_tool_use.py"
POLICY = ROOT / "config" / "command_policy.json"


def load_pre():
    spec = importlib.util.spec_from_file_location("pre_reliability", PRE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_policy_off_environment_value_cannot_disable_controls(monkeypatch):
    pre = load_pre()
    monkeypatch.setenv("TB_COMMAND_POLICY", "off")
    assert pre._enforcement({"enforcement": "block"}) == "block"


def test_reliability_control_files_are_protected():
    pre = load_pre()
    protected = pre.PROTECTED_BOUNDARY_PATHS
    assert "src/agent_control/reliability.py" in protected
    assert "src/agent_control/completion.py" in protected
    assert "config/command_policy.json" in protected


def test_policy_has_no_agent_controlled_off_mode():
    import json
    policy = json.loads(POLICY.read_text())
    assert "off" not in policy["_enforcement_values"]
