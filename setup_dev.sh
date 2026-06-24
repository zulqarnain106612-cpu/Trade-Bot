#!/usr/bin/env bash
# setup_dev.sh — One-shot dev environment setup for Linux/macOS
# GAP-009 fix: Linux/macOS equivalent of setup_dev.ps1
# Run from project root: bash setup_dev.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo
echo '=== Trade Bot Dev Setup (Linux/macOS) ==='
echo

# ── 1. Python check ──────────────────────────────────────────────────────────
PYTHON=""
for candidate in python3.11 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=${ver%%.*}
        minor=${ver##*.}
        if [[ $major -eq 3 && $minor -ge 11 ]]; then
            PYTHON="$candidate"
            echo "[1/8] Found Python $ver at $(command -v $candidate)"
            break
        fi
    fi
done
if [[ -z "$PYTHON" ]]; then
    echo "ERROR: Python 3.11+ not found. Install it and re-run."
    exit 1
fi

# ── 2. Create / activate venv ────────────────────────────────────────────────
echo "[2/8] Setting up .venv..."
if [[ ! -f .venv/bin/activate ]]; then
    "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# ── 3. Upgrade pip + install core deps ───────────────────────────────────────
echo "[3/8] Installing core dependencies..."
pip install --upgrade pip --quiet
if [[ -f requirements.lock ]]; then
    pip install --require-hashes -r requirements.lock --quiet
else
    pip install -r requirements.txt --quiet
fi

# ── 4. Install dev tools ─────────────────────────────────────────────────────
echo "[4/8] Installing dev tools (ruff, mypy, pyright, bandit, pre-commit)..."
pip install ruff mypy pyright bandit semgrep pre-commit pip-tools --quiet

# ── 5. Type stubs ────────────────────────────────────────────────────────────
echo "[5/8] Installing type stubs..."
pip install pandas-stubs types-requests types-PyYAML types-python-dateutil --quiet

# ── 6. Install pre-commit hooks ──────────────────────────────────────────────
echo "[6/8] Installing pre-commit hooks..."
pre-commit install

# ── 7. Frontend deps ─────────────────────────────────────────────────────────
echo "[7/8] Installing frontend dependencies..."
if command -v npm &>/dev/null && [[ -f frontend/package.json ]]; then
    (cd frontend && npm ci --prefer-offline --quiet)
else
    echo '  Skipping frontend (npm not found or no package.json)'
fi

# ── 8. Verify ────────────────────────────────────────────────────────────────
echo "[8/8] Verifying installation..."
python -c 'import fastapi, ccxt, xgboost, hmmlearn, pandas, numpy; print("Core imports OK")'
ruff --version
pre-commit --version

echo
echo '=== Setup complete ==='
echo 'Activate with: source .venv/bin/activate'
echo 'Run tests:     pytest tests/ -q'
echo 'Run linter:    ruff check src/ tests/'
