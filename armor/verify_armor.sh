#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# ARMOR HEALTH CHECK — Run anytime to verify all protection layers are active
# Usage: bash verify_armor.sh [PROJECT_DIR]
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

PROJECT_DIR="${1:-$HOME/Projects/Trade-Bot-main}"
PASS=0; FAIL=0

check() {
  local label="$1" result="$2"
  if [[ "$result" == "ok" ]]; then
    echo "  ✅  ${label}"
    ((PASS++)) || true
  else
    echo "  ❌  ${label}: ${result}"
    ((FAIL++)) || true
  fi
}

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  TRADEBOT ARMOR HEALTH CHECK                                 ║"
echo "╚══════════════════════════════════════════════════════════════╝"

echo ""
echo "── Layer 1: auditd ──────────────────────────────────────────────"
systemctl is-active auditd &>/dev/null && check "auditd service" "ok" || check "auditd service" "NOT RUNNING — run: sudo systemctl start auditd"
[[ -f /etc/audit/rules.d/tradebot.rules ]] && check "auditd rules file" "ok" || check "auditd rules file" "MISSING — re-run install_armor.sh"
auditctl -l 2>/dev/null | grep -q "tradebot" && check "auditd rules loaded" "ok" || check "auditd rules loaded" "NOT LOADED — run: sudo auditctl -R /etc/audit/rules.d/tradebot.rules"

echo ""
echo "── Layer 2: sentinel ────────────────────────────────────────────"
systemctl is-active tradebot-sentinel &>/dev/null && check "sentinel service" "ok" || check "sentinel service" "NOT RUNNING — run: sudo systemctl start tradebot-sentinel"
[[ -f "${PROJECT_DIR}/.armor/sentinel.py" ]] && check "sentinel script" "ok" || check "sentinel script" "MISSING"
[[ -f "${PROJECT_DIR}/.armor/alerts.log" ]] && check "alerts log exists" "ok" || check "alerts log" "NOT YET CREATED (will create on first event)"
[[ -f "${PROJECT_DIR}/.armor/integrity.json" ]] && check "integrity DB" "ok" || check "integrity DB" "MISSING — sentinel not started yet"
pgrep -f "sentinel.py" &>/dev/null && check "sentinel process running" "ok" || check "sentinel process" "NOT IN PROCESS LIST"

echo ""
echo "── Layer 3: git hooks ───────────────────────────────────────────"
GH="${PROJECT_DIR}/.git/hooks"
[[ -x "${GH}/pre-commit" ]] && check "pre-commit hook" "ok" || check "pre-commit hook" "MISSING or not executable"
[[ -x "${GH}/pre-push" ]] && check "pre-push hook" "ok" || check "pre-push hook" "MISSING or not executable"
[[ -x "${GH}/post-commit" ]] && check "post-commit hook" "ok" || check "post-commit hook" "MISSING or not executable"

# Verify hooks owned by root (tamper-resistant)
hook_owner=$(stat -c '%U' "${GH}/pre-commit" 2>/dev/null || echo "unknown")
[[ "$hook_owner" == "root" ]] && check "hooks owned by root" "ok" || check "hooks owned by root" "OWNED BY ${hook_owner} — run: sudo chown root:root ${GH}/pre-commit ${GH}/pre-push ${GH}/post-commit"

echo ""
echo "── Layer 4: VSCode settings ─────────────────────────────────────"
SETTINGS="${PROJECT_DIR}/.vscode/settings.json"
[[ -f "$SETTINGS" ]] && check "settings.json exists" "ok" || check "settings.json" "MISSING"
if [[ -f "$SETTINGS" ]]; then
  grep -q '"git.autofetch": false' "$SETTINGS" && check "git.autofetch disabled" "ok" || check "git.autofetch" "STILL ENABLED — re-apply settings.json"
  grep -q '"git.postCommitCommand": "none"' "$SETTINGS" && check "git.postCommitCommand=none" "ok" || check "git.postCommitCommand" "STILL AUTO-PUSHING"
  grep -q '"python.terminal.activateEnvironment": false' "$SETTINGS" && check "python auto-activate disabled" "ok" || check "python auto-activate" "STILL ENABLED"
  grep -q '"terminal.integrated.shellIntegration.enabled": false' "$SETTINGS" && check "shell integration disabled" "ok" || check "shell integration" "STILL ENABLED"
fi

TASKS="${PROJECT_DIR}/.vscode/tasks.json"
[[ -f "$TASKS" ]] && check "tasks.json exists" "ok" || check "tasks.json" "MISSING"
if [[ -f "$TASKS" ]]; then
  grep -q "autocommit" "$TASKS" && check "autocommit.sh" "STILL PRESENT IN TASKS — remove it" || check "autocommit.sh removed from tasks" "ok"
  grep -q "autofix" "$TASKS" && check "autofix.sh" "STILL PRESENT IN TASKS — remove it" || check "autofix.sh removed from tasks" "ok"
fi

echo ""
echo "── Recent alerts (last 10) ──────────────────────────────────────"
ALERT_LOG="${PROJECT_DIR}/.armor/alerts.log"
if [[ -f "$ALERT_LOG" ]]; then
  tail -10 "$ALERT_LOG" | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        lvl = d.get('level','?')
        color = '\033[91m' if lvl == 'CRITICAL' else '\033[93m' if lvl == 'WARNING' else '\033[0m'
        print(f\"  {color}[{lvl}] {d.get('ts','')} — {d.get('event','')} — {d.get('path','')}\033[0m\")
    except:
        print(f'  {line}')
"
else
  echo "  (no alerts log yet)"
fi

echo ""
echo "══════════════════════════════════════════════════════════════════"
echo "  PASSED: ${PASS}  FAILED: ${FAIL}"
echo "══════════════════════════════════════════════════════════════════"
echo ""
[[ $FAIL -gt 0 ]] && exit 1 || exit 0
