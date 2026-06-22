#!/usr/bin/env bash
# ============================================================
# Project Intelligence — Full Automation Installer
# Run ONCE:
#   bash install_full_auto.sh /home/fujitsu/Projects/Trade-Bot-main
#
# After this, NOTHING needs manual execution:
# - New terminal → context auto-loaded
# - File saved → intel auto-updated
# - Agent responds → state auto-updated
# - New session → project state auto-detected
# ============================================================

set -e

PROJECT="${1:-}"
if [[ -z "$PROJECT" ]]; then
    echo "Usage: bash install_full_auto.sh /path/to/your/project"
    exit 1
fi

PROJECT="$(realpath "$PROJECT")"
if [[ ! -d "$PROJECT" ]]; then
    echo "Error: $PROJECT does not exist"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.config/project-intel"
INTEL_DIR="$PROJECT/.project-intel"

echo ""
echo "════════════════════════════════════════════════════════"
echo "  Project Intelligence — Full Automation Setup"
echo "  Project: $PROJECT"
echo "════════════════════════════════════════════════════════"
echo ""

# ── 1. Install Python deps ───────────────────────────────────────────────────
echo "Step 1/7: Installing Python dependencies..."
pip install watchdog --quiet --break-system-packages 2>/dev/null || \
pip install watchdog --quiet --user 2>/dev/null || true
echo "  ✓ watchdog installed"

# ── 2. Create config directory ───────────────────────────────────────────────
echo "Step 2/7: Creating config directories..."
mkdir -p "$INSTALL_DIR/daemon"
mkdir -p "$INSTALL_DIR/logs"
mkdir -p "$INTEL_DIR/scripts"

# ── 3. Copy daemon scripts ───────────────────────────────────────────────────
echo "Step 3/7: Installing daemon scripts..."
cp "$SCRIPT_DIR/daemon/intel_daemon.py"   "$INSTALL_DIR/daemon/"
cp "$SCRIPT_DIR/daemon/auto_prompt.py"    "$INSTALL_DIR/daemon/"
cp "$SCRIPT_DIR/daemon/output_monitor.py" "$INSTALL_DIR/daemon/"
cp "$SCRIPT_DIR/daemon/output_router.py"  "$INSTALL_DIR/daemon/"
cp "$SCRIPT_DIR/daemon/primer_template.py" "$INSTALL_DIR/daemon/"

# Copy extractor scripts into project
cp "$SCRIPT_DIR/scripts/extract_intelligence.py" "$INTEL_DIR/scripts/"
cp "$SCRIPT_DIR/scripts/update_session.py"       "$INTEL_DIR/scripts/"

echo "  ✓ Daemon scripts installed to $INSTALL_DIR/daemon/"

# ── 4. Create systemd user service ──────────────────────────────────────────
echo "Step 4/7: Setting up systemd service (auto-start on login)..."

SERVICE_FILE="$HOME/.config/systemd/user/project-intel.service"
mkdir -p "$HOME/.config/systemd/user"

cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Project Intelligence Daemon
After=default.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $INSTALL_DIR/daemon/intel_daemon.py $PROJECT
Restart=on-failure
RestartSec=5
StandardOutput=append:$INSTALL_DIR/logs/daemon.log
StandardError=append:$INSTALL_DIR/logs/daemon.log
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$INSTALL_DIR/daemon

[Install]
WantedBy=default.target
EOF

# Enable and start the service
systemctl --user daemon-reload 2>/dev/null || true
systemctl --user enable project-intel 2>/dev/null && \
systemctl --user start project-intel 2>/dev/null && \
echo "  ✓ Systemd service active (auto-starts on login)" || \
echo "  ⚠ Systemd not available — daemon will start via shell hook instead"

# ── 5. Install shell hook ────────────────────────────────────────────────────
echo "Step 5/7: Installing shell hooks (auto session detection)..."

HOOK_LINE="# Project Intelligence Hook"
HOOK_SOURCE="source $SCRIPT_DIR/hooks/shell_hook.sh"

install_hook() {
    local rcfile="$1"
    if [[ -f "$rcfile" ]]; then
        if ! grep -q "Project Intelligence Hook" "$rcfile"; then
            echo "" >> "$rcfile"
            echo "$HOOK_LINE" >> "$rcfile"
            echo "$HOOK_SOURCE" >> "$rcfile"
            echo "  ✓ Hook added to $rcfile"
        else
            echo "  ✓ Hook already in $rcfile"
        fi
    fi
}

install_hook "$HOME/.bashrc"
install_hook "$HOME/.zshrc"
install_hook "$HOME/.bash_profile"

# ── 6. Install CLI commands ──────────────────────────────────────────────────
echo "Step 6/7: Installing CLI commands..."

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"

# intel-route command (auto-routes agent output)
cat > "$BIN_DIR/intel-route" << EOF
#!/usr/bin/env bash
# Route agent output: diagnostics/gaps/issues → project files, chat → terminal
PYTHONPATH="$INSTALL_DIR/daemon" python3 "$INSTALL_DIR/daemon/output_router.py" "\$@"
EOF
chmod +x "$BIN_DIR/intel-route"

# intel-prompt command
cat > "$BIN_DIR/intel-prompt" << EOF
#!/usr/bin/env bash
# Auto-prepend context to any agent prompt
PYTHONPATH="$INSTALL_DIR/daemon" python3 "$INSTALL_DIR/daemon/auto_prompt.py" "\$@"
EOF
chmod +x "$BIN_DIR/intel-prompt"

# intel-capture command (pipe agent output through)
cat > "$BIN_DIR/intel-capture" << EOF
#!/usr/bin/env bash
# Parse agent output and auto-update session state
PYTHONPATH="$INSTALL_DIR/daemon" python3 "$INSTALL_DIR/daemon/output_monitor.py" --pipe "\$@"
EOF
chmod +x "$BIN_DIR/intel-capture"

# intel-status command
cat > "$BIN_DIR/intel-status" << EOF
#!/usr/bin/env bash
PYTHONPATH="$INSTALL_DIR/daemon" python3 - << 'PYEOF'
import json, sys
from pathlib import Path

def find_root(start=Path.cwd()):
    check = start.resolve()
    while check != check.parent:
        if (check / ".project-intel").exists():
            return check
        check = check.parent
    return None

root = find_root()
if not root:
    print("No intel project found in current directory tree")
    sys.exit(1)

state_file = root / ".project-intel" / "SESSION_STATE.json"
state = json.loads(state_file.read_text()) if state_file.exists() else {}
print(f"Project:       {root}")
print(f"Status:        {state.get('session_status', 'unknown')}")
print(f"Last updated:  {state.get('last_updated', 'unknown')}")
print(f"Focus:         {state.get('current_focus', 'not set')}")
print(f"Next task:     {state.get('next_recommended_task', 'check OPEN_TASKS.md')}")
print(f"Last modified: {state.get('last_files_modified', [])}")
print(f"Sessions:      {state.get('total_sessions', 0)}")
PYEOF
EOF
chmod +x "$BIN_DIR/intel-status"

# intel-reload command
cat > "$BIN_DIR/intel-reload" << EOF
#!/usr/bin/env bash
# Force re-extract project intelligence
python3 "$INTEL_DIR/scripts/extract_intelligence.py" "$PROJECT"
echo "Intelligence layer reloaded"
EOF
chmod +x "$BIN_DIR/intel-reload"

echo "  ✓ CLI commands installed: intel-prompt, intel-capture, intel-status, intel-reload"

# Ensure ~/.local/bin is in PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> "$HOME/.bashrc"
    echo "  ✓ Added ~/.local/bin to PATH"
fi

# ── 7. Run initial extraction ────────────────────────────────────────────────
echo "Step 7/7: Running initial intelligence extraction..."
python3 "$INTEL_DIR/scripts/extract_intelligence.py" "$PROJECT"

# ── Write CLAUDE.md ──────────────────────────────────────────────────────────
if [[ ! -f "$PROJECT/CLAUDE.md" ]]; then
    cp "$SCRIPT_DIR/templates/CLAUDE.md" "$PROJECT/CLAUDE.md"
    echo "  ✓ CLAUDE.md written (auto-loaded by Claude Projects)"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo "  ✓ FULL AUTOMATION ACTIVE"
echo "════════════════════════════════════════════════════════"
echo ""
echo "What happens automatically from now on:"
echo ""
echo "  When you open a terminal in $PROJECT"
echo "    → CONTEXT_PRIMER auto-printed, session marked"
echo ""
echo "  When you save any .py/.js file"
echo "    → Intel layer auto-updated (4s debounce)"
echo ""
echo "  When using the CLI:"
echo "    intel-prompt 'implement entropy gate'    # auto-wraps with full context"
echo "    intel-prompt 'fix the slippage model' --clipboard  # copies to clipboard"
echo "    intel-status                              # current project state"
echo "    intel-reload                              # force re-extract"
echo ""
echo "  For chat UIs (Claude.ai, Copilot):"
echo "    intel-prompt 'your task here' --clipboard"
echo "    Then paste in the chat — full context already included"
echo ""
echo "  Daemon logs: $INSTALL_DIR/logs/daemon.log"
echo "  Daemon status: systemctl --user status project-intel"
echo ""
echo "  Reload your shell: source ~/.bashrc"
echo ""
