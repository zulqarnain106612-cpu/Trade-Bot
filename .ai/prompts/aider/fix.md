# Fix Mode Prompt — Aider

You are fixing a bug in Trade-Bot-main.

## Process
1. Reproduce: identify the minimal failing case
2. Root cause: trace to origin, not symptom
3. Fix: minimal, targeted change — no refactoring during bug fix
4. Verify: run `uv run pytest tests/ -x -q` after fix
5. Lint: run `uv run ruff check --fix` + `uv run mypy src/`

## Rules
- One commit per fix
- Add a regression test that fails before fix, passes after
- Commit message: `fix(<scope>): <what was wrong and how fixed>`
- Never "fix" by suppressing errors or widening exception handlers
