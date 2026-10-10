// Minimal preload — no Node, filesystem or shell access is exposed to the
// renderer. The dashboard talks to the FastAPI backend directly over
// HTTP/WS; the only privileged surface is the terminal relay below, which
// forwards validated frames to the terminal service through the main
// process (electron/terminalBridge.cjs). It cannot run anything by itself:
// every frame is checked against an allow-list in main, and the daemon
// validates it again.
const { contextBridge, ipcRenderer } = require("electron");

function listen(channel, callback) {
  const handler = (_event, payload) => callback(payload);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
}

contextBridge.exposeInMainWorld("tradeBotDesktop", {
  isElectron: true,
  // Renderer -> main: report the current pending-approvals count so the
  // main process can update the tray icon/tooltip and OS taskbar badge.
  setPendingApprovals: count => ipcRenderer.send("badge:pending-approvals", count),
  terminal: {
    connect: () => ipcRenderer.invoke("terminal:connect"),
    send: frame => ipcRenderer.send("terminal:send", frame),
    disconnect: () => ipcRenderer.send("terminal:disconnect"),
    onFrame: callback => listen("terminal:frame", callback),
    onStatus: callback => listen("terminal:status", callback),
  },
});
