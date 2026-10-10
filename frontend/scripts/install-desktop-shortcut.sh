#!/usr/bin/env bash
# Installs a Linux app-drawer entry + Desktop shortcut for the Trade Bot
# Electron app, and the integrated terminal service (`tradebot-term`). Run
# once after `npm install` (and again to upgrade); the entry launches through
# `npm run electron:dev`.
#
# The packaged-AppImage branch below is kept for a release/ directory built
# elsewhere: electron-builder was removed in SEC-0007 (every published
# http-cache-semantics is vulnerable), so no npm script packages the app.
#
# Usage: npm run desktop:install   (from frontend/)
set -euo pipefail

FRONTEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ICON_SRC="$FRONTEND_DIR/build/icon.png"
APPIMAGE="$(find "$FRONTEND_DIR/release" -maxdepth 1 -iname '*.AppImage' 2>/dev/null | head -n1 || true)"

APPS_DIR="$HOME/.local/share/applications"
ICONS_DIR="$HOME/.local/share/icons/hicolor/512x512/apps"
DESKTOP_DIR="$HOME/Desktop"

mkdir -p "$APPS_DIR" "$ICONS_DIR"

if [[ ! -f "$ICON_SRC" ]]; then
  echo "Icon not found at $ICON_SRC — run 'npm run icons' first." >&2
  exit 1
fi
cp -f "$ICON_SRC" "$ICONS_DIR/tradebot-dashboard.png"

if [[ -n "$APPIMAGE" ]]; then
  LAUNCH_CMD="$APPIMAGE"
elif [[ -x "$FRONTEND_DIR/node_modules/.bin/electron" ]]; then
  # Dev-mode fallback: launches vite + electron together.
  LAUNCH_CMD="/usr/bin/env bash -lc 'cd \"$FRONTEND_DIR\" && npm run electron:dev'"
else
  echo "No AppImage found in release/ and electron not installed." >&2
  echo "Run 'npm install' first, or drop a packaged AppImage into release/." >&2
  exit 1
fi

sed \
  -e "s|__LAUNCH_CMD__|${LAUNCH_CMD}|" \
  -e "s|__ICON_PATH__|${ICONS_DIR}/tradebot-dashboard.png|" \
  "$FRONTEND_DIR/linux/tradebot-dashboard.desktop" > "$APPS_DIR/tradebot-dashboard.desktop"
chmod +x "$APPS_DIR/tradebot-dashboard.desktop"

if [[ -d "$DESKTOP_DIR" ]]; then
  cp -f "$APPS_DIR/tradebot-dashboard.desktop" "$DESKTOP_DIR/tradebot-dashboard.desktop"
  chmod +x "$DESKTOP_DIR/tradebot-dashboard.desktop"
fi

command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true

echo "Installed app-drawer entry: $APPS_DIR/tradebot-dashboard.desktop"
[[ -d "$DESKTOP_DIR" ]] && echo "Installed desktop shortcut: $DESKTOP_DIR/tradebot-dashboard.desktop"

# Integrated terminal service (TERM-008): the `tradebot-term` host CLI in
# ~/.local/bin and, where `systemctl --user` works, a user unit that keeps the
# service running. Re-running this script is the upgrade. Only files carrying
# the installer's marker are ever rewritten; nothing here touches .env, keys or
# the terminal access token. Set TB_TERMINAL_NO_SYSTEMD=1 to skip the unit
# (the desktop app then starts the service on demand).
REPO_DIR="$(cd "$FRONTEND_DIR/.." && pwd)"
PYTHON="${TB_TERMINAL_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
    PYTHON="$REPO_DIR/.venv/bin/python"
  else
    PYTHON="$(command -v python3 || true)"
  fi
fi
if [[ -z "$PYTHON" ]]; then
  echo "warning: no Python interpreter found; the integrated terminal was not installed." >&2
  echo "         Create the virtualenv (see README), then re-run: npm run desktop:install" >&2
  exit 0
fi
TERMINAL_ARGS=(install)
[[ "${TB_TERMINAL_NO_SYSTEMD:-}" == "1" ]] && TERMINAL_ARGS+=(--no-systemd)
if ! (cd "$REPO_DIR" && PYTHONPATH="$REPO_DIR" "$PYTHON" -m src.terminal "${TERMINAL_ARGS[@]}"); then
  echo "warning: the integrated terminal service was not fully installed (see above)." >&2
  echo "         The dashboard still works; run 'tradebot-term install' to retry." >&2
fi
