# Trade-Bot-main — Project Context

## Overview
Algorithmic trading bot with Python backend and JS/TS frontend.

## Stack
| Layer | Technology |
|-------|-----------|
| Runtime | Python 3.14 |
| Package mgr | uv 0.11.x |
| Linter | ruff |
| Type checker | mypy |
| Tests | pytest |
| Node | v26.3.0 (nvm) |
| VCS | git 2.53.0 |
| Container | Docker 29.6.1 |
| Venv | .venv (uv-managed) |

## Directory Layout
```
Trade-Bot-main/
├── src/            # Python source (do not mutate without instruction)
├── tests/          # pytest test suite
├── frontend/       # JS/TS frontend
├── scripts/        # Utility scripts
├── data/           # Runtime data (excluded from git)
├── logs/           # Log output (excluded from git)
├── models/         # ML models (excluded from git)
├── .ai/            # AI workspace (this directory)
├── pyproject.toml  # Single source of truth for Python deps + tool config
├── .env            # Local secrets (never committed)
└── .env.example    # Reference for required env vars
```

## Key Commands
```bash
uv sync                          # Install/sync dependencies
uv run pytest tests/ -x -q      # Run tests
uv run ruff check --fix src/     # Lint + fix
uv run mypy src/                 # Type check
uv run python -m src.main        # Run bot (check actual entrypoint in pyproject.toml)
```

## AI Workspace Commands
```bash
.ai/scripts/aider-session        # Start aider with project config
.ai/scripts/review               # AI code review of staged changes
.ai/scripts/commit               # Lint → test → commit
.ai/scripts/architect            # Architect mode session
.ai/scripts/context-refresh      # Regenerate project context index
```

## Pre-commit Hooks
Active: pre-commit, post-commit, pre-push
Config: .pre-commit-config.yaml
