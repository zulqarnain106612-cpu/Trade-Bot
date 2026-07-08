# Claude AI Configuration — Trade-Bot-main

## Project Identity
- **Type**: Algorithmic trading bot (Python + JS/TS frontend)
- **Stack**: Python 3.14, uv, ruff, mypy, pytest, pre-commit
- **Venv**: `.venv` (uv-managed)

## Behavioral Rules
- Never guess; validate before implementing.
- Production-grade only — no mocks, no placeholders.
- Prefer `uv run` over bare `python3` or `pip`.
- Always check `.env.example` for required env vars before referencing secrets.
- Keep changes backward-compatible; prefer additive over destructive.
- Run `uv run ruff check --fix` + `uv run mypy src/` after every code change.

## Architecture Constraints
- Source lives in `src/` — never mutate without explicit instruction.
- Tests in `tests/` — always run after changes.
- Use `pyproject.toml` as single source of truth for deps.

## Commit Convention
- Format: `<type>(<scope>): <subject>` (conventional commits)
- Types: feat, fix, refactor, test, chore, docs, perf

## Forbidden
- No `requirements.txt` mutations (use `pyproject.toml` + `uv`).
- No direct `pip install` in CI — use `uv sync`.
- Never commit `.env` or secrets.
