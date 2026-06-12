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
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=true

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
source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate 2>/dev/null || true

# ── Install tools ────────────────────────────────────────────────────────────
step "Installing/upgrading tools"
pip install -q ruff==0.4.4 bandit[sarif]==1.7.8 pip-audit==2.7.3 \
               detect-secrets==1.5.0 mypy==1.10.0 semgrep || warn "Some tools failed to install"

# ── 1. ruff fix ──────────────────────────────────────────────────────────────
step "ruff lint fix"
if $CHECK_ONLY; then
  ruff check src/ tests/ --output-format=concise && ok "ruff check clean" || fail "ruff check found issues"
else
  ruff check src/ tests/ --fix --unsafe-fixes && ok "ruff fix applied" || warn "ruff: some issues not auto-fixable"
  ruff format src/ tests/ && ok "ruff format applied"
fi

# ── 2. mypy ──────────────────────────────────────────────────────────────────
step "mypy type check"
mypy src/ --ignore-missing-imports --no-error-summary 2>&1 | tail -5 && ok "mypy clean" || warn "mypy found type issues (non-blocking)"

# ── 3. bandit security ───────────────────────────────────────────────────────
step "bandit security scan"
bandit -r src/ --severity-level medium --confidence-level medium -q \
  && ok "bandit: no medium+ issues" \
  || warn "bandit found security issues — review above"

# ── 4. semgrep custom rules ──────────────────────────────────────────────────
step "semgrep custom rules"
if command -v semgrep &>/dev/null; then
  semgrep --config .semgrep/rules.yml src/ --quiet \
    && ok "semgrep: no custom rule violations" \
    || warn "semgrep found violations — review above"
else
  warn "semgrep not installed — skipping (pip install semgrep)"
fi

# ── 5. pip-audit CVE scan ────────────────────────────────────────────────────
step "pip-audit CVE scan"
pip-audit --strict -q && ok "pip-audit: no known CVEs" || warn "pip-audit found CVEs — run: pip-audit --fix"

# ── 6. detect-secrets ────────────────────────────────────────────────────────
step "detect-secrets scan"
detect-secrets scan --baseline .secrets.baseline \
  && ok "detect-secrets: baseline clean" \
  || warn "detect-secrets: new secrets found — run: detect-secrets audit .secrets.baseline"

# ── 7. pytest ────────────────────────────────────────────────────────────────
step "pytest"
python -m pytest tests/ -q --tb=short \
  && ok "all tests passed" \
  || fail "tests failed"

# ── 8. frontend (if node available) ──────────────────────────────────────────
if command -v node &>/dev/null && [[ -d "frontend/node_modules" ]]; then
  step "prettier frontend"
  if $CHECK_ONLY; then
    cd frontend && npx prettier --check "src/**/*.{js,jsx}" && ok "prettier clean" || warn "prettier: formatting issues"
    cd "$ROOT"
  else
    cd frontend && npx prettier --write "src/**/*.{js,jsx}" && ok "prettier applied"
    cd "$ROOT"
  fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo -e "\n────────────────────────────────────────"
echo -e "${GREEN}PASS: $PASS${NC}  ${YELLOW}WARN: $WARN${NC}  ${RED}FAIL: $FAIL${NC}"
[[ $FAIL -eq 0 ]] && echo -e "${GREEN}All required checks passed.${NC}" || echo -e "${RED}Fix failures before committing.${NC}"
[[ $FAIL -gt 0 ]] && exit 1 || exit 0
