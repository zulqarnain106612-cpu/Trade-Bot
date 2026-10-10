// Electron main-process bridge between the dashboard renderer and the
// terminal service (python -m src.terminal) on this machine.
//
// The renderer keeps contextIsolation, sandbox and no Node integration. It
// gets four narrow calls from the preload (connect, send, disconnect, and
// two listeners); this module does the rest in the main process:
//
//   * starts the service if it is not running (`tradebot-term start --json`,
//     which uses the systemd user unit when installed and otherwise spawns
//     the daemon detached);
//   * connects to its Unix socket -- a 0600 socket in a 0700 directory whose
//     peer credentials the daemon checks -- so no token ever reaches the
//     renderer;
//   * relays frames, refusing anything that is not a plain object of an
//     allowed request type, or that is larger than the daemon would accept.
//     Job registration (job.*) is not on the list: it is for local programs,
//     never for page content.
const fs = require("fs");
const net = require("net");
const path = require("path");
const { execFile } = require("child_process");

const ALLOWED_TYPES = new Set([
  "hello",
  "ping",
  "session.create",
  "session.attach",
  "session.detach",
  "session.input",
  "session.resize",
  "session.rename",
  "session.signal",
  "session.close",
  "process.kill",
  "process.output",
]);
const MAX_FRAME_BYTES = 1 << 20;

function validateFrame(frame) {
  if (frame === null || typeof frame !== "object" || Array.isArray(frame)) return null;
  if (Object.getPrototypeOf(frame) !== Object.prototype && Object.getPrototypeOf(frame) !== null) return null;
  if (typeof frame.t !== "string" || !ALLOWED_TYPES.has(frame.t)) return null;
  let line;
  try {
    line = JSON.stringify(frame);
  } catch {
    return null;
  }
  if (Buffer.byteLength(line, "utf8") > MAX_FRAME_BYTES) return null;
  return line;
}

function resolvePython(repoRoot, env, exists = fs.existsSync) {
  if (env.TB_TERMINAL_PYTHON) return env.TB_TERMINAL_PYTHON;
  const venv = path.join(repoRoot, ".venv", "bin", "python");
  return exists(venv) ? venv : "python3";
}

function runTerminalCli(args, { repoRoot, env, execFileImpl = execFile, timeoutMs = 20000 }) {
  const python = resolvePython(repoRoot, env);
  const childEnv = {
    ...env,
    PYTHONPATH: env.PYTHONPATH ? `${repoRoot}${path.delimiter}${env.PYTHONPATH}` : repoRoot,
  };
  return new Promise((resolve) => {
    execFileImpl(
      python,
      ["-m", "src.terminal", ...args],
      { cwd: repoRoot, env: childEnv, timeout: timeoutMs },
      (error, stdout, stderr) => {
        resolve({
          code: error ? (typeof error.code === "number" ? error.code : 1) : 0,
          stdout: String(stdout || ""),
          stderr: String(stderr || error?.message || ""),
        });
      },
    );
  });
}

async function ensureService(options) {
  const result = await runTerminalCli(["start", "--json"], options);
  if (result.code !== 0) {
    return { ok: false, error: (result.stderr || "the terminal service did not start").trim() };
  }
  try {
    const info = JSON.parse(result.stdout.trim().split("\n").pop());
    if (typeof info.socket !== "string" || !info.socket) throw new Error("no socket path");
    return { ok: true, socket: info.socket, version: info.version, clientVersion: info.client_version };
  } catch (error) {
    return { ok: false, error: `unexpected reply from tradebot-term start: ${error.message}` };
  }
}

class TerminalBridge {
  constructor({ ipcMain, repoRoot, env, netImpl = net, execFileImpl = execFile, log = console }) {
    this.ipcMain = ipcMain;
    this.repoRoot = repoRoot;
    this.env = env;
    this.netImpl = netImpl;
    this.execFileImpl = execFileImpl;
    this.log = log;
    this.connections = new Map();
    this.service = null;
    this.starting = null;
  }

  register() {
    this.ipcMain.handle("terminal:connect", (event) => this.connect(event.sender));
    this.ipcMain.on("terminal:send", (event, frame) => this.send(event.sender, frame));
    this.ipcMain.on("terminal:disconnect", (event) => this.disconnect(event.sender.id));
  }

  startService() {
    // One start at a time: the window's first connect usually races the
    // warm-up started at launch, and both should wait on the same attempt.
    if (!this.starting) {
      this.starting = ensureService({
        repoRoot: this.repoRoot,
        env: this.env,
        execFileImpl: this.execFileImpl,
      }).then((service) => {
        this.service = service;
        this.starting = null;
        if (!service.ok) this.log.warn?.(`terminal service unavailable: ${service.error}`);
        return service;
      });
    }
    return this.starting;
  }

  async connect(sender) {
    this.disconnect(sender.id);
    const service = this.service?.ok ? this.service : await this.startService();
    if (!service.ok) return { ok: false, error: service.error };
    return new Promise((resolve) => {
      const socket = this.netImpl.createConnection({ path: service.socket });
      const record = { socket, buffer: "" };
      let settled = false;
      socket.setEncoding("utf8");
      socket.on("connect", () => {
        settled = true;
        this.connections.set(sender.id, record);
        resolve({ ok: true, version: service.version });
      });
      socket.on("data", (chunk) => {
        record.buffer += chunk;
        let newline = record.buffer.indexOf("\n");
        while (newline >= 0) {
          const line = record.buffer.slice(0, newline);
          record.buffer = record.buffer.slice(newline + 1);
          newline = record.buffer.indexOf("\n");
          if (!line) continue;
          let frame;
          try { frame = JSON.parse(line); } catch { continue; }
          if (!sender.isDestroyed()) sender.send("terminal:frame", frame);
        }
      });
      socket.on("error", (error) => {
        if (!settled) {
          settled = true;
          // The cached socket path may be stale (the daemon was restarted
          // elsewhere); the next connect asks `start` again.
          this.service = null;
          resolve({ ok: false, error: error.message });
        }
      });
      socket.on("close", () => {
        // Only the current connection may report "closed": a socket replaced
        // by a reconnect, or one the renderer asked to drop, closes silently,
        // or its late close would tear down the connection that replaced it.
        if (this.connections.get(sender.id) !== record) return;
        this.connections.delete(sender.id);
        if (!sender.isDestroyed()) {
          sender.send("terminal:status", { state: "closed", reason: "service connection closed" });
        }
      });
      sender.once?.("destroyed", () => this.disconnect(sender.id));
    });
  }

  send(sender, frame) {
    const line = validateFrame(frame);
    const record = this.connections.get(sender.id);
    if (!line || !record) {
      if (!sender.isDestroyed()) {
        sender.send("terminal:frame", {
          t: "error",
          id: frame && typeof frame === "object" ? frame.id ?? null : null,
          code: line ? "not_connected" : "refused_by_bridge",
          message: line ? "the terminal bridge is not connected" : "frame refused by the desktop bridge",
        });
      }
      return false;
    }
    record.socket.write(`${line}\n`);
    return true;
  }

  disconnect(id) {
    const record = this.connections.get(id);
    if (record) {
      this.connections.delete(id);
      record.socket.destroy();
    }
  }

  closeAll() {
    for (const id of [...this.connections.keys()]) this.disconnect(id);
  }
}

module.exports = {
  ALLOWED_TYPES,
  MAX_FRAME_BYTES,
  TerminalBridge,
  ensureService,
  resolvePython,
  runTerminalCli,
  validateFrame,
};
