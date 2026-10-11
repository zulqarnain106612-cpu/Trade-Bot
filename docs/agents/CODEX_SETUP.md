# Codex setup for Trade-Bot

This repository supports Codex through the project-scoped .codex/config.toml, root AGENTS.md, repository-local skills, a local plugin marketplace, and the official OpenAI developer-docs MCP server. Codex CLI and the VS Code extension share local Codex configuration. Project settings are applied only after the repository is trusted in the Codex client.

## Included configuration

- AGENTS.md: repository engineering contract for Codex; directs the agent to the more detailed existing CLAUDE.md and repository governance.
- .codex/config.toml: approval-on-request, workspace-write sandbox, live web search, official read-only OpenAI developer documentation MCP, and local plugin enablement.
- .agents/skills/: reusable repository-specific evidence-first and delivery workflows.
- .agents/plugins/marketplace.json and plugins/trade-bot-engineering/: a local plugin package with the same core workflow and official OpenAI docs MCP connection.
- .vscode/extensions.json: recommends the official OpenAI ChatGPT/Codex extension alongside the repository's existing extensions.

## Codex CLI and VS Code

1. Install/update the official Codex CLI only if you intend to use the terminal interface; the VS Code extension can run without a separately installed codex command. Sign in through the supported Codex account flow. Do not place API keys or OAuth tokens in this repository.
2. Open this repository in VS Code and trust it only if you intend to run its code and project settings. Open the Codex extension, start a new session, and verify that it picks up the repository instructions.
3. The .codex/config.toml settings are project-scoped. The MCP server is optional (required = false) so a docs-service outage does not prevent coding sessions. Confirm it with the client's MCP/server listing UI or codex mcp list when the CLI is installed.
4. The workspace-write sandbox allows editing within the project, while approval_policy = on-request keeps approval prompts available. Do not change to unrestricted/danger-full-access to bypass repository controls.
5. Skills are in .agents/skills/. Invoke them explicitly by name when useful, or let Codex select them from their descriptions.

## Local plugin marketplace (Codex desktop / supported Work surfaces)

The repository includes a local marketplace named trade-bot-local with a plugin named trade-bot-engineering. Restart the ChatGPT desktop app after opening the repository marketplace, then open the Plugins Directory, choose the local repository marketplace, and install the plugin. The project config enables the plugin for trusted Codex project sessions. A local marketplace is not the same thing as publishing an administrator-managed workspace plugin.

## Developer platform / Build with Codex

Repository configuration does not create a developer-platform project, enable API billing, create an API key, or configure account-level Build with Codex features. If you separately build an API-powered Codex integration or use the official Codex GitHub Action, create the needed platform project and secret in the appropriate account UI. Store credentials as local ignored environment values or GitHub Actions secrets (for example, OPENAI_API_KEY); never commit them, put them in .codex/config.toml, or copy them into agent prompts. Codex CLI signed in through ChatGPT does not require this repository to contain an API key.

## Administration: plugins and connections

A repository cannot change organization/workspace administration settings. An authorized workspace admin must separately review and, if desired, publish the local plugin from the supported Plugins UI, choose its permitted workspace roles, and configure any account-level connections. The bundled OpenAI developer-docs MCP is a public, read-only documentation endpoint and does not require an API key. Do not connect GitHub write access, exchange credentials, or production services merely to enable the repository instructions.

## Verification

From the repository root, run:

    python3 - <<'PY'
    import json, tomllib
    from pathlib import Path
    root = Path.cwd()
    with (root / '.codex/config.toml').open('rb') as f:
        tomllib.load(f)
    for name in ('.agents/plugins/marketplace.json', 'plugins/trade-bot-engineering/plugin.json', 'plugins/trade-bot-engineering/mcp.json'):
        json.loads((root / name).read_text())
    print('Codex TOML and plugin JSON syntax: PASS')
    PY

This validates file syntax and package references, not the live Codex client, plugin installation, MCP connectivity, developer-platform API credentials, or workspace-admin publishing. Verify those in the relevant client/account UI after pushing and checking out the branch.
