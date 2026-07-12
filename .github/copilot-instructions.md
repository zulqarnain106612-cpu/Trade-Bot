# GitHub Copilot — Trade Bot

## Session start
```bash
uv run pytest tests/ -x -q
git log --oneline -5
```
Stack: Python 3.11 | uv | FastAPI | XGBoost+HMM | ccxt (Binance/OKX) | pytest | ruff + mypy

## Never
- go for (simple, partial, minimal, demo, assumpted, incomplete) approaches
- Read: `.env`, `.venv/`, `data/`, `logs/`, `models/`, `requirements.lock`, `rag.db`
- Push to git — commit only
- Use print() in src/ — use structlog
- Hardcode secrets or credentials
