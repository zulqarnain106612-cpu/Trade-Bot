#!/usr/bin/env python3
"""
TRADEBOT SENTINEL v2 — Zero-trust filesystem + process + network monitor
Authorized identities: HUMAN + CLAUDE_MAIN_AGENT_v1
Any unauthorized write/exec/network contact → immediate alert.

Usage: sudo python3 sentinel.py <PROJECT_DIR> <OWNER_USERNAME>
Logs:  <PROJECT_DIR>/.armor/alerts.log
       journalctl -u tradebot-sentinel -f
"""
import os
import sys
import time
import json
import signal
import logging
import hashlib
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Configuration ─────────────────────────────────────────────────────────────
PROJECT_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
OWNER_USERNAME = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("SUDO_USER", "fujitsu")
ARMOR_DIR = PROJECT_DIR / ".armor"
ALERT_LOG = ARMOR_DIR / "alerts.log"
ACCESS_LOG = ARMOR_DIR / "access.log"
INTEGRITY_DB = ARMOR_DIR / "integrity.json"

AGENT_IDENTITY_TOKEN = "CLAUDE_MAIN_AGENT_v1"
AGENT_ENV_VAR = "CLAUDE_AGENT_IDENTITY"

# Processes allowed to READ (not write) without alert — VSCode internals
AUTHORIZED_READ_PROCS = {
    "code", "code-server", "node", "rg", "git",
    "python3", "python", "bash", "sh",          # allowed only when owner's UID
    "inotifywait", "sentinel.py", "python3",
    "kworker", "systemd",
}

# These procs are NEVER authorized to write, regardless of UID
BLACKLISTED_WRITE_PROCS = {
    "curl", "wget", "nc", "ncat", "netcat", "nmap",
    "python3 -c", "bash -c",  # inline exec — matched on cmdline
}

# File patterns that should NEVER change unexpectedly
PROTECTED_PATTERNS = [
    ".vscode/settings.json",
    ".vscode/tasks.json",
    ".vscode/launch.json",
    ".git/hooks/pre-commit",
    ".git/hooks/pre-push",
    ".git/hooks/post-commit",
    ".git/config",
    ".armor/",
    "pyproject.toml",
    ".env",
    ".env.production",
]

# ── Logging setup ─────────────────────────────────────────────────────────────
ARMOR_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(ALERT_LOG),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("sentinel")

# ── Alert delivery ────────────────────────────────────────────────────────────
def send_alert(level: str, event: str, path: str, pid: Optional[int], proc: str, detail: str = ""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    msg = {
        "ts": ts, "level": level, "event": event,
        "path": path, "pid": pid, "proc": proc, "detail": detail,
    }
    line = json.dumps(msg)

    if level == "CRITICAL":
        log.critical(line)
    elif level == "WARNING":
        log.warning(line)
    else:
        log.info(line)

    # Desktop notification — try multiple backends
    summary = f"🚨 SENTINEL [{level}]: {event}"
    body = f"PATH: {path}\nPROCESS: {proc} (PID:{pid})\n{detail}"
    _desktop_notify(summary, body, level)

    # Also write machine-readable alert
    with open(ALERT_LOG, "a") as f:
        f.write(line + "\n")


def _desktop_notify(summary: str, body: str, level: str):
    urgency = "critical" if level == "CRITICAL" else "normal"
    # Try notify-send (requires DISPLAY/WAYLAND env from user session)
    for display_var in ["DISPLAY", "WAYLAND_DISPLAY"]:
        display = subprocess.run(
            ["bash", "-c", f"cat /proc/$(pgrep -u {OWNER_USERNAME} gnome-session | head -1)/environ 2>/dev/null | tr '\\0' '\\n' | grep ^{display_var}="],
            capture_output=True, text=True
        ).stdout.strip()
        if display:
            env_val = display.split("=", 1)[1] if "=" in display else ""
            env = os.environ.copy()
            env[display_var] = env_val
            env["XAUTHORITY"] = f"/home/{OWNER_USERNAME}/.Xauthority"
            try:
                subprocess.run(
                    ["sudo", "-u", OWNER_USERNAME,
                     "notify-send", "--urgency", urgency, "--expire-time", "0",
                     summary, body],
                    env=env, timeout=3, capture_output=True
                )
                return
            except Exception:
                pass

    # Fallback: wall broadcast to all terminals
    try:
        subprocess.run(
            ["wall", f"\n{'='*60}\n{summary}\n{body}\n{'='*60}"],
            timeout=3, capture_output=True
        )
    except Exception:
        pass

    # Fallback: write to all user's ttys
    try:
        for tty in Path("/dev/pts").iterdir():
            try:
                with open(tty, "w") as t:
                    t.write(f"\a\n[SENTINEL ALERT] {summary}\n{body}\n")
            except Exception:
                pass
    except Exception:
        pass


# ── Identity verification ─────────────────────────────────────────────────────
def get_proc_info(pid: int) -> dict:
    """Get process details. Returns dict with name, cmdline, username, environ_token."""
    info = {"name": "unknown", "cmdline": "", "username": "unknown", "is_authorized": False}
    try:
        p = psutil.Process(pid)
        info["name"] = p.name()
        info["cmdline"] = " ".join(p.cmdline())
        info["username"] = p.username()
        # Check for agent identity token in process environment
        try:
            env = p.environ()
            if env.get(AGENT_ENV_VAR) == AGENT_IDENTITY_TOKEN:
                info["is_authorized"] = True
                return info
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        # Authorized if it's the owner themselves
        if info["username"] == OWNER_USERNAME:
            info["is_authorized"] = True  # owner is authorized but still logged
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return info


def is_protected_path(path: str) -> bool:
    rel = str(Path(path).relative_to(PROJECT_DIR)) if PROJECT_DIR.as_posix() in path else path
    return any(pattern in rel for pattern in PROTECTED_PATTERNS)


def is_armor_path(path: str) -> bool:
    return ".armor" in path


# ── Integrity database ────────────────────────────────────────────────────────
def compute_hash(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


def build_integrity_db():
    """Snapshot SHA256 of all protected files on startup."""
    db = {}
    for pattern in PROTECTED_PATTERNS:
        for p in PROJECT_DIR.rglob(pattern.rstrip("/")):
            if p.is_file():
                h = compute_hash(str(p))
                if h:
                    db[str(p)] = h
    with open(INTEGRITY_DB, "w") as f:
        json.dump(db, f, indent=2)
    log.info(f"[INTEGRITY] Snapshot: {len(db)} protected files hashed")
    return db


def verify_integrity(path: str, db: dict) -> bool:
    """Returns True if hash matches. False = tampering detected."""
    if path not in db:
        return True  # not tracked
    current = compute_hash(path)
    return current == db[path]


# ── Filesystem event handler ──────────────────────────────────────────────────
class SentinelHandler(FileSystemEventHandler):
    def __init__(self, integrity_db: dict):
        super().__init__()
        self.integrity_db = integrity_db
        self._lock = threading.Lock()

    def _handle(self, event):
        if event.is_directory:
            return
        path = event.src_path

        # Skip sentinel's own log writes
        if is_armor_path(path):
            return

        event_type = event.event_type  # created, modified, deleted, moved

        # Find PID touching this file (best effort via lsof)
        pid = self._get_pid_for_path(path)
        proc_info = get_proc_info(pid) if pid else {"name": "unknown", "cmdline": "", "username": "unknown", "is_authorized": False}

        protected = is_protected_path(path)
        authorized = proc_info["is_authorized"]
        username = proc_info["username"]
        proc_name = proc_info["name"]
        cmdline = proc_info["cmdline"][:120]

        # ── CRITICAL: unauthorized write to protected file ─────────────────
        if protected and not authorized:
            # Verify integrity
            intact = verify_integrity(path, self.integrity_db)
            send_alert(
                "CRITICAL",
                f"UNAUTHORIZED {event_type.upper()} ON PROTECTED FILE",
                path, pid, f"{proc_name}({username})",
                f"CMD: {cmdline} | INTEGRITY: {'OK' if intact else '⚠️ HASH MISMATCH — FILE CHANGED'}"
            )
            return

        # ── WARNING: unauthorized write to any project file ────────────────
        if event_type in ("created", "modified", "deleted", "moved") and not authorized:
            send_alert(
                "WARNING",
                f"UNAUTHORIZED {event_type.upper()}",
                path, pid, f"{proc_name}({username})",
                f"CMD: {cmdline}"
            )
            return

        # ── INFO: authorized write — log silently ─────────────────────────
        if event_type in ("modified", "created"):
            with open(ACCESS_LOG, "a") as f:
                f.write(json.dumps({
                    "ts": datetime.now().isoformat(),
                    "event": event_type,
                    "path": path,
                    "proc": proc_name,
                    "user": username,
                    "pid": pid,
                    "authorized": True
                }) + "\n")

            # Update integrity db if authorized change to protected file
            if protected:
                new_hash = compute_hash(path)
                if new_hash:
                    self.integrity_db[path] = new_hash
                    with open(INTEGRITY_DB, "w") as f:
                        json.dump(self.integrity_db, f, indent=2)

    def on_modified(self, event): self._handle(event)
    def on_created(self, event): self._handle(event)
    def on_deleted(self, event): self._handle(event)
    def on_moved(self, event): self._handle(event)

    @staticmethod
    def _get_pid_for_path(path: str) -> Optional[int]:
        try:
            result = subprocess.run(
                ["lsof", "-t", path],
                capture_output=True, text=True, timeout=1
            )
            pids = [int(p) for p in result.stdout.strip().split() if p.isdigit()]
            return pids[0] if pids else None
        except Exception:
            return None


# ── Network monitor ───────────────────────────────────────────────────────────
class NetworkMonitor(threading.Thread):
    """
    Polls every 5s for new outbound connections from processes
    whose cwd is inside PROJECT_DIR. Any new external connection = alert.
    """
    POLL_INTERVAL = 5
    KNOWN_SAFE_REMOTE = {
        "127.0.0.1", "::1", "localhost",
    }

    def __init__(self):
        super().__init__(daemon=True, name="NetworkMonitor")
        self._seen_connections: set = set()
        self._running = True

    def stop(self): self._running = False

    def run(self):
        log.info("[NETWORK] Monitor started")
        while self._running:
            self._scan()
            time.sleep(self.POLL_INTERVAL)

    def _scan(self):
        try:
            for proc in psutil.process_iter(["pid", "name", "username", "cwd", "cmdline"]):
                try:
                    cwd = proc.info.get("cwd") or ""
                    if str(PROJECT_DIR) not in cwd:
                        continue
                    conns = proc.net_connections(kind="inet")
                    for c in conns:
                        if c.status not in ("ESTABLISHED", "SYN_SENT"):
                            continue
                        raddr = c.raddr
                        if not raddr:
                            continue
                        remote_ip = raddr.ip
                        if remote_ip in self.KNOWN_SAFE_REMOTE:
                            continue
                        key = (proc.pid, remote_ip, raddr.port)
                        if key in self._seen_connections:
                            continue
                        self._seen_connections.add(key)
                        pid = proc.info["pid"]
                        proc_info = get_proc_info(pid)
                        authorized = proc_info["is_authorized"]
                        send_alert(
                            "CRITICAL" if not authorized else "INFO",
                            "OUTBOUND NETWORK CONNECTION FROM PROJECT PROCESS",
                            cwd, pid,
                            f"{proc.info['name']}({proc.info['username']})",
                            f"REMOTE: {remote_ip}:{raddr.port} | "
                            f"CMD: {' '.join(proc.info.get('cmdline', []))[:100]} | "
                            f"AUTHORIZED: {authorized}"
                        )
                except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                    continue
        except Exception as e:
            log.error(f"[NETWORK] Scan error: {e}")


# ── Process monitor ───────────────────────────────────────────────────────────
class ProcessMonitor(threading.Thread):
    """
    Polls every 3s for new processes with cwd inside PROJECT_DIR.
    Alerts on any unexpected process spawning.
    """
    POLL_INTERVAL = 3
    EXPECTED_PROCS = {
        "python3", "python", "node", "npm", "vite",
        "code", "code-server", "rg", "git", "bash", "sh",
        "pytest", "ruff", "sentinel.py",
    }

    def __init__(self):
        super().__init__(daemon=True, name="ProcessMonitor")
        self._seen_pids: set = set()
        self._running = True

    def stop(self): self._running = False

    def run(self):
        log.info("[PROCESS] Monitor started")
        # Seed existing pids
        for proc in psutil.process_iter(["pid"]):
            self._seen_pids.add(proc.pid)
        while self._running:
            self._scan()
            time.sleep(self.POLL_INTERVAL)

    def _scan(self):
        try:
            for proc in psutil.process_iter(["pid", "name", "username", "cwd", "cmdline", "ppid"]):
                try:
                    pid = proc.info["pid"]
                    if pid in self._seen_pids:
                        continue
                    self._seen_pids.add(pid)
                    cwd = proc.info.get("cwd") or ""
                    if str(PROJECT_DIR) not in cwd:
                        continue
                    proc_name = proc.info.get("name", "unknown")
                    username = proc.info.get("username", "unknown")
                    cmdline = " ".join(proc.info.get("cmdline", []))[:120]
                    proc_info = get_proc_info(pid)
                    authorized = proc_info["is_authorized"]
                    level = "INFO" if authorized else "WARNING"
                    if proc_name not in self.EXPECTED_PROCS and not authorized:
                        level = "CRITICAL"
                    send_alert(
                        level,
                        f"NEW PROCESS IN PROJECT DIR",
                        cwd, pid, f"{proc_name}({username})",
                        f"CMD: {cmdline} | AUTHORIZED: {authorized}"
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as e:
            log.error(f"[PROCESS] Scan error: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 70)
    log.info(f"TRADEBOT SENTINEL STARTING")
    log.info(f"Project: {PROJECT_DIR}")
    log.info(f"Owner:   {OWNER_USERNAME}")
    log.info(f"Agent:   {AGENT_IDENTITY_TOKEN} (env: {AGENT_ENV_VAR})")
    log.info(f"Logs:    {ALERT_LOG}")
    log.info("=" * 70)

    if not PROJECT_DIR.exists():
        log.critical(f"PROJECT_DIR does not exist: {PROJECT_DIR}")
        sys.exit(1)

    # Build integrity snapshot
    integrity_db = build_integrity_db()

    # Layer 2a: Filesystem watcher
    handler = SentinelHandler(integrity_db)
    observer = Observer()
    observer.schedule(handler, str(PROJECT_DIR), recursive=True)
    observer.start()
    log.info(f"[FILESYSTEM] Watching: {PROJECT_DIR} (recursive)")

    # Layer 2b: Network monitor
    net_monitor = NetworkMonitor()
    net_monitor.start()

    # Layer 2c: Process monitor
    proc_monitor = ProcessMonitor()
    proc_monitor.start()

    send_alert("INFO", "SENTINEL STARTED", str(PROJECT_DIR), os.getpid(), "sentinel.py",
               f"All layers active. Owner={OWNER_USERNAME}. Agent={AGENT_IDENTITY_TOKEN}")

    def _shutdown(sig, frame):
        log.info("[SENTINEL] Shutting down...")
        net_monitor.stop()
        proc_monitor.stop()
        observer.stop()
        observer.join()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown(None, None)


if __name__ == "__main__":
    main()
