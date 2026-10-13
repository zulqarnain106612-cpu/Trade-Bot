"""Keep Trade-Bot project skills and its installable Codex plugin consistent."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _read_json(relative_path: str) -> dict[str, Any]:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def test_codex_and_plugin_manifests_reference_existing_assets() -> None:
    with (ROOT / ".codex/config.toml").open("rb") as stream:
        config = tomllib.load(stream)
    assert "mcp_servers" in config
    marketplace = _read_json(".agents/plugins/marketplace.json")
    plugin_entries = marketplace["plugins"]
    plugin = next(
        entry for entry in plugin_entries if entry["name"] == "trade-bot-engineering"
    )
    plugin_path = ROOT / plugin["source"]["path"].removeprefix("./")
    assert (plugin_path / ".codex-plugin/plugin.json").is_file()
    assert (plugin_path / ".mcp.json").is_file()
    assert (plugin_path / "plugin.json").is_file()
    assert (plugin_path / "mcp.json").is_file()
    manifest_path = plugin_path / ".codex-plugin/plugin.json"
    mcp_config_path = plugin_path / ".mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mcp_config = json.loads(mcp_config_path.read_text(encoding="utf-8"))
    assert manifest["name"] == "trade-bot-engineering"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    developer_docs_url = mcp_config["mcpServers"]["openaiDeveloperDocs"]["url"]
    assert developer_docs_url == "https://developers.openai.com/mcp"


def test_project_skills_are_packaged_without_drift() -> None:
    project_skills = ROOT / ".agents/skills"
    bundled_skills = ROOT / "plugins/trade-bot-engineering/skills"
    project_names = sorted(
        path.name for path in project_skills.iterdir() if (path / "SKILL.md").is_file()
    )
    bundled_names = sorted(
        path.name for path in bundled_skills.iterdir() if (path / "SKILL.md").is_file()
    )
    assert bundled_names == project_names
    for name in project_names:
        project_text = (project_skills / name / "SKILL.md").read_text(encoding="utf-8")
        bundled_text = (bundled_skills / name / "SKILL.md").read_text(encoding="utf-8")
        assert bundled_text == project_text, f"plugin skill has drifted: {name}"
