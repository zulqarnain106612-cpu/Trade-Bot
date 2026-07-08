# Claude Task Prompt — Trade-Bot-main

## Context Injection
Before every task, I am providing:
- `.ai/context/project.md` — architecture overview
- `.ai/prompts/shared/conventions.md` — coding standards

## Task Execution Rules
1. **Validate intent** — restate the goal before acting
2. **Plan** — list files to change and why
3. **Implement** — production-grade, no placeholders
4. **Verify** — list commands to validate the change
5. **Summarize** — what changed, what to watch for

## Forbidden Actions (without explicit approval)
- Deleting or renaming source files
- Changing pyproject.toml dependencies
- Modifying .env
- Pushing to git remote

## Response Format
Keep responses concise. Use code blocks for all code.
No motivational preamble. Start with the plan, then execute.
