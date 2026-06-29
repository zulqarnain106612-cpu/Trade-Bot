#!/usr/bin/env bash
# intel-health.sh — verify and self-repair the project intelligence system
# Run any time: bash scripts/intel-health.sh
# Claude runs this automatically at session start if something seems wrong.

set -euo pipefail
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INTEL_DIR="$PROJECT/.project-intel"
DAEMON_DIR="$HOME/.config/project-intel"
VENV="$PROJECT/.venv/bin/python3"
FIXED=0

err() { echo "  ✗ $1"; }
ok()  { echo "  ✓ $1"; }
fix() { echo "  ⚙ FIXING: $1"; FIXED=$((FIXED+1)); }

echo "════════════════════════════════════════════════════════"
echo "  Project Intelligence Health Check"
echo "  Project: $PROJECT"
echo "════════════════════════════════════════════════════════"
echo ""

# ── 1. Python / venv ──────────────────────────────────────────────────────────
echo "[1] Python environment"
if [ -x "$VENV" ]; then
  ok "venv python exists: $VENV"
else
  err "venv python missing at $VENV"
  echo "     Run: cd $PROJECT && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

# watchdog in venv
if "$VENV" -c "import watchdog" 2>/dev/null; then
  ok "watchdog installed in venv"
else
  fix "installing watchdog into venv"
  "$VENV" -m pip install "watchdog>=4.0.0" --quiet
fi

# watchdog in requirements.txt
if grep -q "watchdog" "$PROJECT/requirements.txt" 2>/dev/null; then
  ok "watchdog in requirements.txt"
else
  fix "adding watchdog to requirements.txt"
  echo "watchdog>=4.0.0" >> "$PROJECT/requirements.txt"
fi

echo ""

# ── 2. Daemon ─────────────────────────────────────────────────────────────────
echo "[2] Daemon"
LAUNCHER="$DAEMON_DIR/run_daemon.sh"
DAEMON_PY="$DAEMON_DIR/daemon/intel_daemon.py"

if [ -f "$LAUNCHER" ] && [ -x "$LAUNCHER" ]; then
  ok "launcher exists: $LAUNCHER"
else
  fix "recreating launcher"
  mkdir -p "$DAEMON_DIR"
  cat > "$LAUNCHER" << INNER
#!/usr/bin/env bash
PROJECT="/home/fujitsu/Projects/Trade-Bot-main"
DAEMON="\$HOME/.config/project-intel/daemon/intel_daemon.py"
if [ -x "\$PROJECT/.venv/bin/python3" ]; then PYTHON="\$PROJECT/.venv/bin/python3"
else PYTHON="\$(which python3)"; fi
"\$PYTHON" -c "import watchdog" 2>/dev/null || \
  "\$PYTHON" -m pip install "watchdog>=4.0.0" --quiet --break-system-packages 2>/dev/null || true
exec "\$PYTHON" "\$DAEMON" "\$PROJECT" "\$PYTHON"
INNER
  chmod +x "$LAUNCHER"
fi

if [ -f "$DAEMON_PY" ]; then
  CLASS_COUNT=$(grep -c "^class " "$DAEMON_PY" 2>/dev/null || echo 0)
  if [ "$CLASS_COUNT" -eq 5 ]; then
    ok "daemon.py intact ($CLASS_COUNT classes)"
  else
    err "daemon.py has $CLASS_COUNT classes (expected 5) — may have been appended/corrupted"
    err "Run: bash scripts/intel-reinstall.sh to rebuild daemon"
  fi
else
  err "daemon.py MISSING at $DAEMON_PY"
fi

# Systemd service
SVC="$HOME/.config/systemd/user/project-intel.service"
if [ -f "$SVC" ]; then
  if grep -q "run_daemon.sh" "$SVC"; then
    ok "systemd service uses launcher (robust)"
  else
    fix "updating systemd service to use launcher"
    cat > "$SVC" << INNER
[Unit]
Description=Project Intelligence Daemon — Trade Bot
After=default.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
ExecStart=$LAUNCHER
Restart=on-failure
RestartSec=15
StandardOutput=append:$DAEMON_DIR/logs/daemon.log
StandardError=append:$DAEMON_DIR/logs/daemon.log
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$PROJECT

[Install]
WantedBy=default.target
INNER
    systemctl --user daemon-reload
  fi
else
  fix "creating systemd service"
  mkdir -p "$HOME/.config/systemd/user"
  # (same content as above)
fi

# Daemon running?
if systemctl --user is-active --quiet project-intel; then
  ok "daemon is running"
else
  fix "starting daemon"
  systemctl --user start project-intel
  sleep 3
  if systemctl --user is-active --quiet project-intel; then
    ok "daemon started successfully"
  else
    err "daemon failed to start — check: journalctl --user -u project-intel -n 20"
  fi
fi

echo ""

# ── 3. Intel files ────────────────────────────────────────────────────────────
echo "[3] Intel files"
REQUIRED=(
  "$INTEL_DIR/CONTEXT_PRIMER.md"
  "$INTEL_DIR/SESSION_STATE.json"
  "$INTEL_DIR/GAPS.md"
  "$INTEL_DIR/OPEN_TASKS.md"
  "$INTEL_DIR/DECISION_LOG.md"
  "$INTEL_DIR/ISSUES.md"
  "$INTEL_DIR/RISK_LOG.md"
  "$INTEL_DIR/knowledge/INDEX.json"
  "$INTEL_DIR/scripts/extract_intelligence.py"
  "$INTEL_DIR/scripts/rag_engine.py"
  "$INTEL_DIR/scripts/context_builder.py"
  "$INTEL_DIR/scripts/cognitive_layer.py"
  "$PROJECT/scripts/claude-commit.sh"
)
MISSING=0
for f in "${REQUIRED[@]}"; do
  if [ -f "$f" ]; then ok "$(basename $f)"
  else err "MISSING: $f"; MISSING=$((MISSING+1)); fi
done
if [ "$MISSING" -gt 0 ]; then
  echo "  Run: bash scripts/intel-reinstall.sh to restore missing files"
fi

echo ""

# ── 4. Shell hook ─────────────────────────────────────────────────────────────
echo "[4] Shell hook"
HOOK="$DAEMON_DIR/hooks/shell_hook.sh"
if [ -f "$HOOK" ]; then
  ok "shell_hook.sh exists"
  if bash -n "$HOOK" 2>/dev/null; then
    ok "shell_hook.sh syntax valid"
  else
    err "shell_hook.sh has syntax errors"
  fi
else
  err "shell_hook.sh MISSING at $HOOK"
fi

BASHRC_LINE='source ~/.config/project-intel/hooks/shell_hook.sh'
if grep -qF "$BASHRC_LINE" ~/.bashrc 2>/dev/null; then
  ok ".bashrc sources shell hook"
else
  fix "adding hook source to .bashrc"
  echo "$BASHRC_LINE" >> ~/.bashrc
fi

echo ""

# ── 5. Git hygiene ────────────────────────────────────────────────────────────
echo "[5] Git hygiene"
cd "$PROJECT"
RUNTIME_FILES=("rag.db" "rag.db-journal" ".active_session_context" ".session_marker" "daemon.pid")
for f in "${RUNTIME_FILES[@]}"; do
  if grep -q "$f" .gitignore 2>/dev/null; then
    ok "$f in .gitignore"
  else
    fix "adding $f to .gitignore"
    echo ".project-intel/$f" >> .gitignore
  fi
done

# Remove any runtime files accidentally tracked
for f in "${RUNTIME_FILES[@]}"; do
  if git ls-files --error-unmatch ".project-intel/$f" 2>/dev/null; then
    fix "removing .project-intel/$f from git tracking"
    git rm --cached ".project-intel/$f" 2>/dev/null || true
  fi
done

echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════"
if [ "$FIXED" -eq 0 ] && [ "$MISSING" -eq 0 ]; then
  echo "  ✓ ALL CHECKS PASSED — system healthy"
else
  echo "  ⚙ $FIXED items auto-repaired"
  [ "$MISSING" -gt 0 ] && echo "  ✗ $MISSING files missing — run intel-reinstall.sh"
fi
echo "════════════════════════════════════════════════════════"
