# Trade-Bot — Claude Desktop Session Entry
<!-- Regenerate: bash .ai/scripts/context-refresh -->

## ⚡ SESSION START — ONE COMMAND, THEN STOP
```bash
python3 .project-intel/scripts/resume.py /home/fujitsu/Projects/Trade-Bot-main
```
That single output is your **complete context**. Do NOT read any other file to orient.
Ask the user what to work on, then read ONLY the 1–3 files the task requires.

## If resume.py fails
```bash
# Stack: Python 3.11 | uv | FastAPI | XGBoost+HMM | ccxt(Binance/OKX) | pytest | ruff+mypy
# Entrypoint: src/api/main.py | Engine: src/engine/orchestrator.py
# Sizing: src/risk/kelly.py | Regime: src/regime/detector.py
uv run pytest tests/ -x -q          # validate health
git log --oneline -3                 # last known state
cat .project-intel/SESSION_STATE.json | python3 -c "import json,sys; s=json.load(sys.stdin); print('NEXT:', s['next_recommended_task'][:160])"
```

## Absolute rules
- **Never** read files to orient — resume.py is the context
- **Never** read: `.env`, `GAPS.md`, `ARCHITECTURE.md`, `MODULE_MAP.json`, `RAW_SCAN.json`, `rag.db`, `requirements.lock`, `node_modules/`, `.venv/`, `data/`, `logs/`, `models/`
- **Never** `cat` any file >100 lines — use `grep`/`sed`/`head`/`tail` + line ranges
- **Always** `uv run` not bare `python3`/`pip`
- **Always** run `uv run ruff check --fix src/ && uv run mypy src/ && uv run pytest tests/ -x -q` after code changes
- **Commit**: `bash scripts/claude-commit.sh --msg "type(scope): desc [claude]"`
