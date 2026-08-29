#!/usr/bin/env bash
# scripts/autofix.sh — One-command local auto-fix + security check
#
# Usage:
#   ./scripts/autofix.sh          # fix + check everything
#   ./scripts/autofix.sh --check  # check only, no writes
#
# Tools run (in order, all auto-installed if missing):
#   1. ruff --fix       — lint fixes (imports, style, pyupgrade, bugbear)
#   2. ruff format      — code formatting
#   3. bandit           — security lint (reports only)
#   4. semgrep          — custom trading security rules (reports only)
#   5. pip-audit        — CVE scan (reports only)
#   6. detect-secrets   — credential scan (reports only)
#   7. mypy             — type check (reports only)
#   8. pytest           — test suite
#   9. prettier         — frontend formatting
#
# Auto-fixes: ruff + prettier only (deterministic, safe)
# Reports: all others — human must review and act

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CHECK_ONLY=false
if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=true
fi

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0; WARN=0

step() { echo -e "\n${YELLOW}▶ $*${NC}"; }
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; ((PASS++)); }
fail() { echo -e "${RED}  ✗ $*${NC}"; ((FAIL++)); }
warn() { echo -e "${YELLOW}  ⚠ $*${NC}"; ((WARN++)); }

# ── Ensure venv ──────────────────────────────────────────────────────────────
if [[ ! -d ".venv" ]]; then
  step "Creating virtualenv"
  python3 -m venv .venv
fi
# shellcheck source=.venv/bin/activate
if ! source .venv/bin/activate 2>/dev/null; then
  if ! source .venv/Scripts/activate 2>/dev/null; then
    true
  fi
fi

# ── Install tools ────────────────────────────────────────────────────────────
step "Installing/upgrading tools"
if ! pip install -q ruff==0.4.4 bandit[sarif]==1.7.8 pip-audit==2.7.3 \
               detect-secrets==1.5.0 mypy==1.10.0 semgrep; then
  warn "Some tools failed to install"
fi

# ── 1. ruff fix ──────────────────────────────────────────────────────────────
step "ruff lint fix"
if [[ "$CHECK_ONLY" == "true" ]]; then
  if ruff check src/ tests/ --output-format=concise; then
    ok "ruff check clean"
  else
    fail "ruff check found issues"
  fi
else
  if ruff check src/ tests/ --fix --unsafe-fixes; then
    ok "ruff fix applied"
  else
    warn "ruff: some issues not auto-fixable"
  fi
  if ruff format src/ tests/; then
    ok "ruff format applied"
  fi
fi

# ── 2. mypy ──────────────────────────────────────────────────────────────────
step "mypy type check"
if mypy src/ --ignore-missing-imports --no-error-summary 2>&1 | tail -5; then
  ok "mypy clean"
else
  warn "mypy found type issues (non-blocking)"
fi

# ── 3. bandit security ───────────────────────────────────────────────────────
step "bandit security scan"
if bandit -r src/ --severity-level medium --confidence-level medium -q; then
  ok "bandit: no medium+ issues"
else
  warn "bandit found security issues — review above"
fi

# ── 4. semgrep custom rules ──────────────────────────────────────────────────
step "semgrep custom rules"
if command -v semgrep &>/dev/null; then
  if semgrep --config .semgrep/rules.yml src/ --quiet; then
    ok "semgrep: no custom rule violations"
  else
    warn "semgrep found violations — review above"
  fi
else
  warn "semgrep not installed — skipping (pip install semgrep)"
fi

# ── 5. pip-audit CVE scan ────────────────────────────────────────────────────
step "pip-audit CVE scan"
if pip-audit --strict -q; then
  ok "pip-audit: no known CVEs"
else
  warn "pip-audit found CVEs — run: pip-audit --fix"
fi

# ── 6. detect-secrets ────────────────────────────────────────────────────────
step "detect-secrets scan"
if detect-secrets scan --baseline .secrets.baseline; then
  ok "detect-secrets: baseline clean"
else
  warn "detect-secrets: new secrets found — run: detect-secrets audit .secrets.baseline"
fi

# ── 7. pytest ────────────────────────────────────────────────────────────────
step "pytest"
if python -m pytest tests/ -q --tb=short; then
  ok "all tests passed"
else
  fail "tests failed"
fi

# ── 8. frontend (if node available) ──────────────────────────────────────────
if command -v node &>/dev/null && [[ -d "frontend/node_modules" ]]; then
  step "prettier frontend"
if [[ "$CHECK_ONLY" == "true" ]]; then
    cd frontend
    if npx prettier --check "src/**/*.{js,jsx}"; then
      ok "prettier clean"
    else
      warn "prettier: formatting issues"
    fi
    cd "$ROOT"
  else
    cd frontend
    if npx prettier --write "src/**/*.{js,jsx}"; then
      ok "prettier applied"
    fi
    cd "$ROOT"
  fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo -e "\n────────────────────────────────────────"
echo -e "${GREEN}PASS: $PASS${NC}  ${YELLOW}WARN: $WARN${NC}  ${RED}FAIL: $FAIL${NC}"
if [[ $FAIL -eq 0 ]]; then
  echo -e "${GREEN}All required checks passed.${NC}"
else
  echo -e "${RED}Fix failures before committing.${NC}"
fi
if [[ $FAIL -gt 0 ]]; then
  exit 1
else
  exit 0
fi
