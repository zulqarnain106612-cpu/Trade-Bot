# Programmatic Tool Calling (PTC) -- adapted for Claude Code (no API)

Source: Anthropic Claude Cookbook,
[Programmatic tool calling (PTC)](https://platform.claude.com/cookbook/tool-use-programmatic-tool-calling-ptc).

See `.claude/skills/programmatic-tool-calling/SKILL.md` for the operational
checklist. Summary: `tools/registry.py` gates which functions a batch script
may import (`orchestratable=True`); everything else stays direct-call-only.
Copy `scripts/orchestration_template.py` per task; print only a digest.
