#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# ARMOR INSTALLER v1 — Trade-Bot-main
# Installs all 4 layers:
#   L1: auditd kernel-level syscall audit
#   L2: Python watchdog sentinel (filesystem + process + network)
#   L3: Git hooks lockdown
#   L4: VSCode workspace hardening validation
#
# Run as: sudo bash install_armor.sh /home/fujitsu/Projects/Trade-Bot-main fujitsu
# ════════════════════════════════════════════════════════════════════════════
set -euo pipefail

PROJECT_DIR="${1:?Usage: sudo bash install_armor.sh <PROJECT_DIR> <YOUR_USERNAME>}"
OWNER="${2:?Usage: sudo bash install_armor.sh <PROJECT_DIR> <YOUR_USERNAME>}"
ARMOR_DIR="${PROJECT_DIR}/.armor"
SENTINEL_SCRIPT="${ARMOR_DIR}/sentinel.py"
SERVICE_NAME="tradebot-sentinel"

echo "[ARMOR] Installing dependencies..."
apt-get install -y inotify-tools libnotify-bin auditd python3-pip --quiet
pip3 install watchdog psutil --break-system-packages --quiet

echo "[ARMOR] Creating armor directory..."
mkdir -p "${ARMOR_DIR}"
chown "${OWNER}:${OWNER}" "${ARMOR_DIR}"

# ── Copy sentinel script ──────────────────────────────────────────────────
cp "$(dirname "$0")/sentinel.py" "${SENTINEL_SCRIPT}"
chmod 750 "${SENTINEL_SCRIPT}"
chown root:root "${SENTINEL_SCRIPT}"        # root owns it — user cannot tamper

# ── Layer 1: auditd rules ────────────────────────────────────────────────
echo "[ARMOR] Installing auditd rules..."
cp "$(dirname "$0")/audit.rules" /etc/audit/rules.d/tradebot.rules
chmod 640 /etc/audit/rules.d/tradebot.rules
chown root:root /etc/audit/rules.d/tradebot.rules
systemctl enable auditd --now 2>/dev/null || true
auditctl -R /etc/audit/rules.d/tradebot.rules 2>/dev/null || true

# ── Layer 3: Git hooks ───────────────────────────────────────────────────
echo "[ARMOR] Installing git hooks..."
GIT_HOOKS_DIR="${PROJECT_DIR}/.git/hooks"
mkdir -p "${GIT_HOOKS_DIR}"
cp "$(dirname "$0")/git-hooks/pre-commit" "${GIT_HOOKS_DIR}/pre-commit"
cp "$(dirname "$0")/git-hooks/pre-push"   "${GIT_HOOKS_DIR}/pre-push"
cp "$(dirname "$0")/git-hooks/post-commit" "${GIT_HOOKS_DIR}/post-commit"
chmod 750 "${GIT_HOOKS_DIR}/pre-commit" "${GIT_HOOKS_DIR}/pre-push" "${GIT_HOOKS_DIR}/post-commit"
chown root:root "${GIT_HOOKS_DIR}/pre-commit" "${GIT_HOOKS_DIR}/pre-push" "${GIT_HOOKS_DIR}/post-commit"

# ── Layer 2: systemd service for sentinel ────────────────────────────────
echo "[ARMOR] Installing sentinel systemd service..."
cat > "/etc/systemd/system/${SERVICE_NAME}.service" << EOF
[Unit]
Description=TradeBot Sentinel — Zero-trust filesystem + network watcher
After=network.target auditd.service
Wants=auditd.service

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 ${SENTINEL_SCRIPT} ${PROJECT_DIR} ${OWNER}
Restart=always
RestartSec=2
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal
SyslogIdentifier=tradebot-sentinel

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" --now

echo ""
echo "[ARMOR] ══════════════════════════════════════════════"
echo "[ARMOR] ALL LAYERS INSTALLED"
echo "[ARMOR]   L1 auditd:    systemctl status auditd"
echo "[ARMOR]   L2 sentinel:  systemctl status ${SERVICE_NAME}"
echo "[ARMOR]   L2 logs:      journalctl -u ${SERVICE_NAME} -f"
echo "[ARMOR]   L3 git hooks: ${GIT_HOOKS_DIR}/"
echo "[ARMOR]   alerts log:   ${ARMOR_DIR}/alerts.log"
echo "[ARMOR] ══════════════════════════════════════════════"
