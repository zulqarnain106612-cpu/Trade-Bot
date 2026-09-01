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
# `--no-save`: a plain install rewrites package-lock.json under this image's npm,
# which would leave every cloud session starting on a dirty tree.
npm install --prefix frontend --no-save --no-audit --no-fund

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

# Preflight: the MongoDB-backed tooling CLAUDE.md points sessions at
# (kg_cli.py, rag_cli.py, review/retrieval.py) needs two things this container
# does not have by default. Report their state now, so a session finds out here
# instead of part-way through a task.
echo "==> Preflight"

if [ -n "${MONGODB_URI:-}" ]; then
  echo "    MONGODB_URI: set -- kg_cli.py / rag_cli.py can reach Atlas"
else
  echo "    MONGODB_URI: NOT set -- kg_cli.py and rag_cli.py will fail."
  echo "                 Add it to the cloud environment's variables to enable them."
  echo "                 (The GitHub Actions secret of the same name covers CI"
  echo "                  review only; it is not visible in this container.)"
fi

# The embedding model is fetched from huggingface.co on first use. Some network
# policies deny it, and then RAG fails on a proxy 403 rather than anything that
# names the real cause.
if ! command -v curl >/dev/null 2>&1; then
  echo "    huggingface.co: not probed (no curl)"
elif curl -fsS -o /dev/null --max-time 10 https://huggingface.co/ 2>/dev/null; then
  echo "    huggingface.co: reachable -- embeddings can download on first use"
else
  echo "    huggingface.co: unreachable -- embedding downloads will fail, so"
  echo "                    rag_cli.py cannot embed. Allow it in the cloud"
  echo "                    environment's network policy to enable RAG."
fi

echo "==> Setup complete"
