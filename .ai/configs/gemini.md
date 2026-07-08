# Gemini CLI Configuration — Trade-Bot-main

## Context
Algorithmic trading bot. Python 3.14, uv, ruff, mypy, pytest.
Frontend: JS/TS. Git-based workflow with conventional commits.

## Preferred Behaviors
- Use `uv run` for all Python execution.
- Validate env vars against `.env.example` before use.
- Keep `src/` layout intact.
- Output diffs, not full file rewrites.
- Always reference `.ai/context/project.md` for architecture.

## Model Preference
gemini-2.5-pro for architecture; gemini-2.5-flash for quick edits.
