# Trade-Bot-main — Shared AI Conventions

## Code Style
- Python: ruff (E/F/I/UP rules), mypy strict, type hints everywhere
- Max line length: 100 (from pyproject.toml)
- Imports: isort via ruff-I, stdlib → third-party → local

## Naming
- snake_case for Python, camelCase for TS/JS
- Classes: PascalCase; constants: UPPER_SNAKE_CASE

## Error Handling
- Never bare `except:`; always catch specific exceptions
- Log errors with structured logging (no print())
- Raise custom domain exceptions from `src/exceptions.py` if it exists

## Testing
- pytest with fixtures; no unittest
- Parametrize edge cases
- Mock external I/O (exchange APIs, DB) — never call real APIs in tests

## Git
- Conventional commits: feat|fix|refactor|test|chore|docs|perf(<scope>): <subject>
- Atomic commits — one logical change per commit
- Never commit: .env, *.pyc, __pycache__, .venv, credentials

## Security
- Secrets via env vars only; load with `python-dotenv`
- No hardcoded API keys, passwords, or tokens anywhere
- Validate all external input before use
