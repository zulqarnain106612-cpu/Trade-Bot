# Claude Code cloud: GitHub-MCP-only setup

## Purpose

Run Trade-Bot work in one managed Claude Code cloud session where GitHub MCP is the only connected MCP server. The repository hook is a second enforcement layer; it is not a substitute for configuring the cloud session itself.

## Required session configuration

1. Create a **new Claude Code cloud session** and select only `zulqarnain106612-cpu/Trade-Bot`. Do not add companion repositories.
2. In the session's connector/MCP configuration, include **GitHub only**. Remove or deselect every other connector, MCP server, computer-use integration, terminal/desktop connector, and external tool provider.
3. If the session is a cloud routine or another unattended workflow, inspect its Connectors list explicitly. Do not assume it inherits a minimal list: Anthropic documents that connected MCP connectors may be included by default.
4. Keep the cloud environment's network access at the most restrictive available setting that still permits the required GitHub MCP integration. Do not add shell, browser, remote-terminal, or arbitrary-network fallback access.
5. Start a fresh session after changing repository or connector configuration. Do not rely on a pre-existing session to refresh its tool inventory.

## Verify before sending the task

- Run `/mcp` and verify that GitHub is the only connected MCP server. If any other server appears, disconnect it and restart the session.
- Inspect the visible tool inventory. GitHub tools should be the only MCP tools available.
- Confirm that the selected repository is exactly `zulqarnain106612-cpu/Trade-Bot`.
- Confirm project settings/hooks are active for this repository. If the session does not load project settings, stop; do not continue under an assumed policy.
- Test a harmless non-GitHub tool attempt only if such a tool remains visible. It must be denied with a redirect to GitHub MCP. A tool absent from the inventory cannot emit a hook denial message because it cannot be called.

## What the repository enforces

- `.claude/settings.json` denies native tools and known alternate MCP server families.
- The wildcard `PreToolUse` hook is the code-owned default-deny gate. Only exact allowlisted GitHub MCP tool names and explicitly scoped Trade-Bot requests can proceed.
- Unknown/future tool names, unscoped or cross-repository requests, CI run/log/artifact/check/status routes, protected policy changes, unsafe branch writes, and destructive operations are denied.
- Successful tool output is passed through the wildcard observation hook before Claude receives it.

## Important boundary: project settings are not host administration

Repository settings cannot remove an MCP server that the cloud host independently exposes, nor can they govern a separate Claude session that does not load this repository's settings. The cloud connector/tool inventory is therefore a required first control layer; the repository hook is defense in depth. Do not treat a denial message as proof that no alternate route exists—verify the session's actual tool inventory.

Do not add `.claude/settings.local.json` to the repository. It is machine/session-specific and is not a reliable way to configure an Anthropic-hosted cloud session. Keep the shared project policy in the committed `.claude/settings.json`, and configure cloud connectors in the cloud host.

## Official Anthropic references

- [Claude Code cloud sessions](https://code.claude.com/docs/en/claude-code-on-the-web)
- [Claude Code permissions](https://code.claude.com/docs/en/permissions)
- [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
