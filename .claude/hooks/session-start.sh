#!/bin/bash
# SessionStart hook: prepare a Claude Code on the web container so that the
# linter, the test suite and the frontend build all run without extra setup.
#
# Runs synchronously, so the session only starts once this finishes. Safe to
# re-run: every step is idempotent.
set -euo pipefail

# Local sessions already have a developer-managed environment; only the remote
# containers need provisioning.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}"

echo "==> Installing Python dependencies (runtime + dev tooling)"
# A virtualenv, not the system interpreter: several runtime deps (cryptography,
# pip itself) are distro-installed in this image without RECORD files, so pip
# aborts with "Cannot uninstall ..." when it tries to replace them.
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade --quiet pip
# requirements.txt is the source of truth for runtime deps; requirements-dev.txt
# pins ruff/pytest. requirements-optional.txt is deliberately NOT installed --
# the heavy ML extras are imported lazily and their suites self-skip, matching CI.
.venv/bin/python -m pip install --quiet -r requirements.txt -r requirements-dev.txt

echo "==> Installing frontend dependencies"
# `npm install` rather than `npm ci` so a warm container cache is reused.
npm install --prefix frontend --no-audit --no-fund

# Put the virtualenv first on PATH so `pytest`, `ruff` and `python` resolve to
# the pinned versions without an explicit `.venv/bin/` prefix or activation.
echo "export PATH=\"$PWD/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
echo "export VIRTUAL_ENV=\"$PWD/.venv\"" >> "$CLAUDE_ENV_FILE"

# Tests import as `from src.<pkg> ...` and the CLIs (kg_cli.py, rag_cli.py,
# orchestrator_cli.py) import first-party packages from the repo root.
echo "export PYTHONPATH=\"$PWD\"" >> "$CLAUDE_ENV_FILE"

# The bot reads its configuration through pydantic-settings. No real credentials
# exist in a sandbox, so seed the non-secret defaults that keep the suite on the
# zero-dependency paths (embedded SQLite, paper trading) and give the security
# settings syntactically valid throwaway values. Anything absent stays absent so
# the fail-open/skip branches behave exactly as they do in CI.
{
  echo 'export TRADING_MODE="paper"'
  echo 'export STORAGE_BACKEND="sqlite"'
  echo 'export BINANCE_TESTNET="true"'
  echo "export API_SECRET_KEY=\"$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')\""
  echo "export OPERATOR_SECRET=\"$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')\""
} >> "$CLAUDE_ENV_FILE"

echo "==> Setup complete"
