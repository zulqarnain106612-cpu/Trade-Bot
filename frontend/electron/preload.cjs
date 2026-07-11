// Minimal preload — no privileged APIs are exposed to the renderer.
// The dashboard talks to the FastAPI backend directly over HTTP/WS,
// so no IPC bridge is required today. Kept as an explicit empty
// contextBridge surface for future native integrations (notifications, etc).
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("tradeBotDesktop", {
  isElectron: true,
  // Renderer -> main: report the current pending-approvals count so the
  // main process can update the tray icon/tooltip and OS taskbar badge.
  setPendingApprovals: count => ipcRenderer.send("badge:pending-approvals", count),
});
