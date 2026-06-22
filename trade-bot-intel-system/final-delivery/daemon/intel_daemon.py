#!/usr/bin/env python3
"""
Project Intelligence Daemon
=============================
Runs as a background service. Does three things automatically:

1. FILE WATCHER: Detects .py/.js/.jsx changes → re-extracts intel
2. SESSION TRACKER: Detects new terminal/chat sessions → marks session boundary
3. STATE WRITER: After any change, writes updated SESSION_STATE.json

Never needs manual intervention. Started once via systemd, runs forever.
"""

import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [daemon] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("intel-daemon")

# ── Constants ─────────────────────────────────────────────────────────────────
SKIP_DIRS = {
    "__pycache__", ".git", "node_modules", ".venv", "venv",
    "dist", "build", ".mypy_cache", ".ruff_cache", "htmlcov",
    ".pytest_cache", ".project-intel"
}
WATCH_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".yml", ".yaml", ".toml"}
DEBOUNCE_SECONDS = 4.0   # wait this long after last change before re-extracting
MIN_REEXTRACT_INTERVAL = 30  # never re-extract more often than this (seconds)


# ── File change handler ───────────────────────────────────────────────────────

class SourceChangeHandler(FileSystemEventHandler):
    def __init__(self, project_root: Path, on_change):
        self.project_root = project_root
        self.on_change = on_change
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _is_relevant(self, path: str) -> bool:
        p = Path(path)
        if any(skip in p.parts for skip in SKIP_DIRS):
            return False
        return p.suffix in WATCH_EXTENSIONS

    def on_modified(self, event):
        if not event.is_directory and self._is_relevant(event.src_path):
            self._debounce(event.src_path)

    def on_created(self, event):
        if not event.is_directory and self._is_relevant(event.src_path):
            self._debounce(event.src_path)

    def _debounce(self, path: str):
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(
                DEBOUNCE_SECONDS,
                self.on_change,
                args=[path]
            )
            self._timer.start()


# ── Session detection ─────────────────────────────────────────────────────────

class SessionTracker:
    """
    Detects new sessions by watching a session-marker file.
    The shell hook (added to .bashrc/.zshrc) touches this file on every
    new terminal open. The daemon detects the touch and marks a new session.
    """
    def __init__(self, intel_dir: Path):
        self.intel_dir = intel_dir
        self.marker_file = intel_dir / ".session_marker"
        self.last_session_time = 0.0
        self.session_count = self._load_session_count()

    def _load_session_count(self) -> int:
        state = self._load_state()
        return state.get("total_sessions", 0)

    def _load_state(self) -> dict:
        state_file = self.intel_dir / "SESSION_STATE.json"
        if state_file.exists():
            try:
                return json.loads(state_file.read_text())
            except Exception:
                pass
        return {}

    def check_new_session(self) -> bool:
        """Returns True if a new session was detected since last check."""
        if not self.marker_file.exists():
            return False
        mtime = self.marker_file.stat().st_mtime
        if mtime > self.last_session_time:
            self.last_session_time = mtime
            self.session_count += 1
            return True
        return False

    def record_session_start(self):
        state = self._load_state()
        state["total_sessions"] = self.session_count
        state["last_session_start"] = datetime.now().isoformat()
        state["session_status"] = "active"
        state["context_primer_ready"] = True
        self._write_state(state)
        log.info(f"New session #{self.session_count} recorded")

    def record_output_delivered(self, summary: str = ""):
        state = self._load_state()
        state["last_output_delivered"] = datetime.now().isoformat()
        state["session_status"] = "output_delivered"
        if summary:
            state["last_output_summary"] = summary
        self._write_state(state)

    def _write_state(self, state: dict):
        state_file = self.intel_dir / "SESSION_STATE.json"
        state_file.write_text(json.dumps(state, indent=2))


# ── Intel extractor wrapper ───────────────────────────────────────────────────

class IntelExtractor:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.intel_dir = project_root / ".project-intel"
        self.extractor = self.intel_dir / "scripts" / "extract_intelligence.py"
        self.last_extract_time = 0.0
        self.last_file_hash = ""

    def _project_hash(self) -> str:
        """Fast hash of all source file mtimes — change detection without reading files."""
        parts = []
        for p in sorted(self.project_root.rglob("*.py")):
            if any(skip in p.parts for skip in SKIP_DIRS):
                continue
            try:
                parts.append(f"{p}:{p.stat().st_mtime}")
            except Exception:
                pass
        return hashlib.md5("|".join(parts).encode()).hexdigest()[:12]

    def extract_if_needed(self, changed_file: str = "") -> bool:
        now = time.time()
        if now - self.last_extract_time < MIN_REEXTRACT_INTERVAL:
            log.debug("Skipping extract — too soon since last run")
            return False

        current_hash = self._project_hash()
        if current_hash == self.last_file_hash:
            log.debug("No structural changes detected — skipping extract")
            return False

        log.info(f"Source changed ({changed_file or 'detected'}) → re-extracting intel...")
        try:
            result = subprocess.run(
                [sys.executable, str(self.extractor), str(self.project_root)],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                self.last_extract_time = now
                self.last_file_hash = current_hash
                log.info("Intel extraction complete")
                # Log what changed
                changed_rel = Path(changed_file).relative_to(self.project_root) if changed_file else "multiple files"
                self._record_extraction(str(changed_rel))
                return True
            else:
                log.error(f"Extraction failed: {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            log.error("Extraction timed out")
        except Exception as e:
            log.error(f"Extraction error: {e}")
        return False

    def _record_extraction(self, trigger_file: str):
        state_file = self.intel_dir / "SESSION_STATE.json"
        try:
            state = json.loads(state_file.read_text()) if state_file.exists() else {}
            state["last_extraction"] = datetime.now().isoformat()
            state["last_extraction_trigger"] = trigger_file
            state_file.write_text(json.dumps(state, indent=2))
        except Exception:
            pass


# ── Main daemon loop ──────────────────────────────────────────────────────────

class IntelDaemon:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self.intel_dir = self.project_root / ".project-intel"
        self.running = True

        self.extractor = IntelExtractor(self.project_root)
        self.session_tracker = SessionTracker(self.intel_dir)
        self.observer = Observer()

        # Signal handling
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT, self._shutdown)

    def _shutdown(self, *_):
        log.info("Daemon shutting down...")
        self.running = False
        self.observer.stop()

    def _on_file_change(self, changed_path: str):
        log.info(f"Change detected: {Path(changed_path).name}")
        self.extractor.extract_if_needed(changed_path)

    def run(self):
        log.info(f"Intel daemon starting for: {self.project_root}")

        # Initial extraction on startup
        log.info("Running initial extraction...")
        self.extractor.extract_if_needed("startup")

        # Start file watcher
        handler = SourceChangeHandler(self.project_root, self._on_file_change)
        self.observer.schedule(handler, str(self.project_root), recursive=True)
        self.observer.start()
        log.info("File watcher active")

        # Write PID file so other tools can find this daemon
        pid_file = self.intel_dir / "daemon.pid"
        pid_file.write_text(str(os.getpid()))

        # Main loop — session detection
        log.info("Session monitor active — daemon ready")
        try:
            while self.running:
                if self.session_tracker.check_new_session():
                    self.session_tracker.record_session_start()
                time.sleep(1.0)
        finally:
            self.observer.stop()
            self.observer.join()
            if pid_file.exists():
                pid_file.unlink()
            log.info("Daemon stopped")


def main():
    if len(sys.argv) < 2:
        print("Usage: intel_daemon.py /path/to/project")
        sys.exit(1)
    root = Path(sys.argv[1])
    if not root.exists():
        print(f"Error: {root} does not exist")
        sys.exit(1)
    IntelDaemon(root).run()


if __name__ == "__main__":
    main()
