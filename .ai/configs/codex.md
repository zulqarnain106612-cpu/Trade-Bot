# Codex / OpenAI CLI Configuration — Trade-Bot-main

## Approval Policy
All shell commands require explicit approval before execution.

## Context
- Project: Python algorithmic trading bot
- Entrypoint: `src/`
- Toolchain: uv, ruff, mypy, pytest

## Command Whitelist (safe to auto-approve)
- `uv run ruff check --fix {files}`
- `uv run mypy src/`
- `uv run pytest tests/ -x -q`
- `git diff`, `git status`, `git log --oneline -10`

## Forbidden (always require manual approval)
- Any command touching `.env`
- `git push`, `git reset --hard`
- `uv add`, `uv remove` (dep changes need review)
