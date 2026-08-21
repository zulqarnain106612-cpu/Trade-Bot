#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════════
# PROJECT SENTINEL v1
# Authorized identities: HUMAN + CLAUDE_MAIN_AGENT_v1
# Any other process touching the project directory triggers an alert.
# Requires: inotifywait (inotify-tools), notify-send (libnotify-bin)
# Install: sudo apt install inotify-tools libnotify-bin
# Usage:   bash project_sentinel.sh [PROJECT_DIR]
# Autostart: add to ~/.config/systemd/user/ (see sentinel.service below)
# ════════════════════════════════════════════════════════════════════════════

set -euo pipefail

PROJECT_DIR="${1:-$HOME/Projects/Trade-Bot-main}"
LOG_FILE="${PROJECT_DIR}/.sentinel/access.log"
AUTHORIZED_PIDS_FILE="${PROJECT_DIR}/.sentinel/authorized_pids"
IDENTITY_TOKEN="CLAUDE_MAIN_AGENT_v1"

mkdir -p "${PROJECT_DIR}/.sentinel"
touch "$LOG_FILE"
touch "$AUTHORIZED_PIDS_FILE"

# ── Register current shell as authorized ─────────────────────────────────────
echo $$ >> "$AUTHORIZED_PIDS_FILE"

alert() {
  local event="$1"
  local path="$2"
  local pid="$3"
  local proc="$4"
  local ts
  ts=$(date '+%Y-%m-%d %H:%M:%S.%3N')

  local msg="[SENTINEL] ${ts} | EVENT: ${event} | PATH: ${path} | PID: ${pid} | PROC: ${proc}"
  echo "$msg" >> "$LOG_FILE"

  # Desktop notification
  notify-send \
    --urgency=critical \
    --expire-time=0 \
    --icon=dialog-warning \
    "🚨 SENTINEL ALERT" \
    "EVENT: ${event}\nPATH: ${path}\nPROCESS: ${proc} (PID:${pid})\nTIME: ${ts}" 2>/dev/null || true

  # Terminal bell + print
  echo -e "\a\033[1;31m${msg}\033[0m"
}

is_authorized() {
  local pid="$1"
  local proc="$2"

  # Authorized: this sentinel itself
  [[ "$pid" == "$$" ]] && return 0

  # Authorized: processes with CLAUDE_MAIN_AGENT_v1 env var set
  if [[ -r "/proc/${pid}/environ" ]]; then
    if grep -qz "CLAUDE_AGENT_IDENTITY=${IDENTITY_TOKEN}" "/proc/${pid}/environ" 2>/dev/null; then
      return 0
    fi
  fi

  # Authorized: known safe VSCode internals (file watching, not execution)
  local safe_procs=("code" "code-server" "node" "rg" "git")
  for safe in "${safe_procs[@]}"; do
    # Only allow VSCode node for READ events, not WRITE/EXEC
    [[ "$proc" == "$safe" ]] && return 0
  done

  return 1
}

echo "[SENTINEL] Watching: ${PROJECT_DIR}"
echo "[SENTINEL] Log: ${LOG_FILE}"
echo "[SENTINEL] Identity token: ${IDENTITY_TOKEN}"
echo "[SENTINEL] Press Ctrl+C to stop."
echo ""

# ── Watch for: create, modify, move, delete, attrib, execute ─────────────────
inotifywait \
  --monitor \
  --recursive \
  --format '%T|%e|%w%f' \
  --timefmt '%Y-%m-%d %H:%M:%S' \
  --event create,modify,moved_to,moved_from,delete,attrib,access,open,close_write,close_nowrite \
  --exclude '\.sentinel/access\.log' \
  "${PROJECT_DIR}" 2>/dev/null \
| while IFS='|' read -r ts events path; do

  # Get PID of last process that touched this path (best effort via lsof)
  pid=$(lsof -t "$path" 2>/dev/null | head -1 || echo "unknown")
  proc="unknown"
  if [[ "$pid" != "unknown" ]] && [[ -r "/proc/${pid}/comm" ]]; then
    proc=$(cat "/proc/${pid}/comm" 2>/dev/null || echo "unknown")
  fi

  # Skip pure read-only events from authorized procs to reduce noise
  if [[ "$events" =~ ^(ACCESS|OPEN|CLOSE_NOWRITE)$ ]]; then
    if is_authorized "$pid" "$proc"; then
      continue
    fi
  fi

  # Alert on any WRITE/CREATE/DELETE/MOVE regardless of who
  if [[ "$events" =~ (CREATE|MODIFY|MOVED|DELETE|CLOSE_WRITE|ATTRIB) ]]; then
    if ! is_authorized "$pid" "$proc"; then
      alert "$events" "$path" "$pid" "$proc"
    else
      # Authorized write — log silently
      echo "[OK] ${ts} | ${events} | ${path} | ${proc}(${pid})" >> "$LOG_FILE"
    fi
  fi

done
