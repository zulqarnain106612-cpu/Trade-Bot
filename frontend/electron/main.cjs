// Electron main process — desktop shell for the Trade Bot dashboard.
// Provides: app window, system tray icon with quick status, minimize-to-tray,
// and native OS integration (dock/taskbar icon, single-instance lock).
const { app, BrowserWindow, Tray, Menu, nativeImage, shell, ipcMain } = require("electron");
const path = require("path");

const isDev = !app.isPackaged;
const DEV_URL = process.env.VITE_DEV_URL || "http://127.0.0.1:5173";

const BUILD_DIR = path.join(__dirname, "..", "build");
const ICON_PATH = path.join(BUILD_DIR, "icon.png");
const TRAY_ICON_PATH = path.join(BUILD_DIR, "tray.png");
const TRAY_BADGE_ICON_PATH = path.join(BUILD_DIR, "tray-badge.png");

let mainWindow = null;
let tray = null;
let isQuitting = false;
let pendingApprovals = 0;

// Single-instance lock — clicking a second launch (e.g. from app drawer)
// just focuses the existing window instead of spawning a duplicate process.
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 640,
    backgroundColor: "#08070a",
    icon: ICON_PATH,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  if (isDev) {
    mainWindow.loadURL(DEV_URL);
  } else {
    mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }

  // Open external links (if any) in the OS browser, not inside the app window.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  // Closing the window hides it to the tray instead of quitting — mirrors
  // typical trading-terminal behavior so the bot keeps monitoring in the tray.
  mainWindow.on("close", event => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
}

function trayIconForBadge(count) {
  const iconPath = count > 0 ? TRAY_BADGE_ICON_PATH : TRAY_ICON_PATH;
  return nativeImage.createFromPath(iconPath);
}

function updateTrayBadge(count) {
  pendingApprovals = Math.max(0, count | 0);
  if (tray) {
    tray.setImage(trayIconForBadge(pendingApprovals));
    tray.setToolTip(
      pendingApprovals > 0
        ? `Trade Bot — ${pendingApprovals} pending approval${pendingApprovals === 1 ? "" : "s"}`
        : "Trade Bot"
    );
  }
  // Unity/GNOME launcher badge (macOS dock badge too, if ever packaged there).
  // No-op on desktop environments that don't implement it.
  if (typeof app.setBadgeCount === "function") {
    app.setBadgeCount(pendingApprovals);
  }
}

function createTray() {
  tray = new Tray(trayIconForBadge(pendingApprovals));
  tray.setToolTip("Trade Bot");

  const menu = Menu.buildFromTemplate([
    {
      label: "Open Dashboard",
      click: () => {
        mainWindow.show();
        mainWindow.focus();
      },
    },
    { type: "separator" },
    {
      label: "Quit Trade Bot",
      click: () => {
        isQuitting = true;
        app.quit();
      },
    },
  ]);
  tray.setContextMenu(menu);

  tray.on("click", () => {
    if (mainWindow.isVisible()) {
      mainWindow.hide();
    } else {
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

ipcMain.on("badge:pending-approvals", (_event, count) => {
  updateTrayBadge(typeof count === "number" ? count : 0);
});

app.whenReady().then(() => {
  createWindow();
  createTray();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
    else mainWindow.show();
  });
});

app.on("before-quit", () => {
  isQuitting = true;
});

// Keep running in the tray on all platforms except macOS default quit-on-close
// behavior is already handled by the close handler above.
app.on("window-all-closed", () => {
  if (process.platform !== "darwin" && isQuitting) app.quit();
});
