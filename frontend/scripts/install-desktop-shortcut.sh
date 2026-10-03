#!/usr/bin/env bash
# Installs a Linux app-drawer entry + Desktop shortcut for the Trade Bot
# Electron app. Run once after `npm install`; the entry launches through
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
