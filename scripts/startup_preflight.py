#!/usr/bin/env python3
"""
Controlled PAPER-mode startup preflight for the API entry point.

    python3 scripts/startup_preflight.py [--port N] [--timeout S] [-- <command>]

Starts the API the way an operator does (by default ``python -m src.api``
with the interpreter running this script, from the repository root), with
three things forced so the run cannot touch money:

  * TRADING_MODE=paper;
  * every exchange credential blanked, so no account can be reached;
  * a fresh API key and operator secret that exist only for this run and are
    never printed (``--api-key-env`` takes the key from a variable instead,
    so a dashboard given the same key can watch the run).

Everything else -- host, port, timeframes, storage paths -- comes from the
configuration the API itself resolves (``--port`` sets API_PORT, the
configured route). It then measures the process from the outside:

  * when the port accepts connections and /livez answers (liveness);
  * the separate API, runtime reconciliation, model/loop, and first-tick
    milestones; a /status-only build is insufficient evidence and fails;
  * the first tick on each timeframe (a structured log line carrying a trace
    id, which only a tick binds);
  * peak RSS of the whole process tree against the runtime monitor's
    critical threshold;
  * a graceful SIGINT shutdown: exit code, duration, and whether the API
    logged its shutdown as complete.

The child's log is parsed, never printed: one JSON summary line is the only
output. Exit 0 when the API became ready, the required timeframes ticked,
memory stayed under the critical threshold and shutdown was clean; 1
otherwise; 2 when the preflight itself could not run.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

CREDENTIAL_ENV = (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "OKX_API_KEY",
    "OKX_API_SECRET",
    "OKX_PASSPHRASE",
)
# The log events the summary reports by first-seen time (seconds from start).
MILESTONE_EVENTS = (
    "orchestrator.startup",
    "orchestrator.selftest_passed",
    "orchestrator.warm_up_deferred",
    "api.runtime_startup_reconcile",
    "api.startup_complete",
    "orchestrator.history_window_refresh",
    "orchestrator.training_start",
    "orchestrator.training_complete",
    "orchestrator.startup_complete",
    "orchestrator.warm_up_complete",
    "orchestrator.warm_up_failed",
    "api.startup_failed",
    "api.shutdown_complete",
)
# A field worth reporting from one log event, by event name.
FACT_FIELDS = {"orchestrator.warm_up_complete": "released_mb"}
MAX_ERRORS_REPORTED = 3


class _LogWatcher:
    """Parses the child's structured log on a thread; keeps only what is summarised."""

    def __init__(self, stream: Any, started: float) -> None:
        self._stream = stream
        self._started = started
        self._lock = threading.Lock()
        self.first_seen: dict[str, float] = {}
        self.first_tick: dict[str, float] = {}
        self.levels: dict[str, int] = {}
        self.errors: list[str] = []
        self.facts: dict[str, Any] = {}
        self.thread = threading.Thread(target=self._run, name="preflight-log", daemon=True)

    def _run(self) -> None:
        for line in self._stream:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                self._note(record)

    def _note(self, record: dict[str, Any]) -> None:
        now = round(time.monotonic() - self._started, 1)
        event = str(record.get("event", ""))
        level = str(record.get("level", "")).lower()
        with self._lock:
            self.first_seen.setdefault(event, now)
            self.levels[level] = self.levels.get(level, 0) + 1
            if event in FACT_FIELDS:
                self.facts[FACT_FIELDS[event]] = record.get(FACT_FIELDS[event])
            timeframe = record.get("timeframe")
            if record.get("trace_id") and isinstance(timeframe, str):
                self.first_tick.setdefault(timeframe, now)
            if level in ("error", "critical") and len(self.errors) < MAX_ERRORS_REPORTED:
                detail = str(record.get("error", ""))[:100]
                self.errors.append(f"{event}: {detail}" if detail else event)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            seen = self.first_seen
            return {
                "milestones_s": {e: seen[e] for e in MILESTONE_EVENTS if e in seen},
                "first_tick_s": dict(self.first_tick),
                "log_levels": dict(self.levels),
                "errors": list(self.errors),
                "facts": dict(self.facts),
            }


def _tree_rss_mb(root_pid: int) -> float:
    """Resident memory of a process and every descendant, in MiB."""
    total_kb = 0
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            status = Path(f"/proc/{pid}/status").read_text()
            for task in Path(f"/proc/{pid}/task").iterdir():
                children = (task / "children").read_text().split()
                pending.extend(int(child) for child in children)
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                total_kb += int(line.split()[1])
    return round(total_kb / 1024.0, 1)


def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _get(port: int, path: str, api_key: str | None) -> tuple[int, Any]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if api_key is not None:
        request.add_header("x-api-key", api_key)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:  # /readyz answers 503 with its report
        try:
            return exc.code, json.loads(exc.read() or b"null")
        except ValueError:
            return exc.code, None
    except (OSError, ValueError):
        return 0, None


def _source_commit_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )
    return result.stdout.strip()


def _tracked_worktree_dirty() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )
    return bool(result.stdout.strip())


def _configured_port() -> int:
    from src.config import get_settings

    return int(get_settings().api.port)


def _model_resource_profile() -> dict[str, int]:
    from src.config import get_settings
    from src.tuning.live_overrides import (
        apply_xgboost_resource_profile,
        effective_xgboost_settings,
    )

    configured = effective_xgboost_settings(get_settings().xgboost)
    effective = apply_xgboost_resource_profile(configured)
    return {
        "configured": {
            "xgb_n_estimators": int(configured.n_estimators),
            "xgb_max_depth": int(configured.max_depth),
            "xgb_n_jobs": int(configured.n_jobs),
        },
        "effective": {
            "xgb_n_estimators": int(effective.n_estimators),
            "xgb_max_depth": int(effective.max_depth),
            "xgb_n_jobs": int(effective.n_jobs),
        },
    }


def _critical_rss_mb() -> float:
    from src.diagnostics.runtime_monitor import MEMORY_CRITICAL_MB

    return float(MEMORY_CRITICAL_MB)


def _repo_path(path: Path) -> Path:
    """Resolve a configured storage path relative to this checkout."""
    return path if path.is_absolute() else REPO_ROOT / path


def _isolate_storage(env: dict[str, str], temp_root: Path) -> dict[str, Any]:
    """Copy local SQLite/model caches into a disposable PAPER-only sandbox.

    The child must never write to the operator's normal database, model
    artifacts, logs, or decision journal. SQLite's backup API gives a
    consistent snapshot even when the source database uses WAL mode.
    """
    from src.config import get_settings

    storage = get_settings().storage
    source_db = _repo_path(Path(storage.db_path))
    isolated_db = temp_root / "data" / "trade_bot.db"
    isolated_db.parent.mkdir(parents=True, exist_ok=True)
    copied_database = False
    if source_db.is_file():
        try:
            with sqlite3.connect(str(source_db), timeout=10) as source:
                with sqlite3.connect(str(isolated_db), timeout=10) as destination:
                    source.backup(destination)
            copied_database = True
        except sqlite3.Error as exc:
            raise RuntimeError(f"could not snapshot local SQLite cache: {exc}") from exc

    source_models = _repo_path(Path(storage.model_dir))
    isolated_models = temp_root / "models" / "artifacts"
    if source_models.is_dir():
        shutil.copytree(source_models, isolated_models, dirs_exist_ok=True)
    else:
        isolated_models.mkdir(parents=True, exist_ok=True)

    isolated_logs = temp_root / "logs"
    isolated_logs.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "STORAGE_BACKEND": "sqlite",
            "STORAGE_DB_PATH": str(isolated_db),
            "STORAGE_MODEL_DIR": str(isolated_models),
            "STORAGE_LOG_DIR": str(isolated_logs),
            "STORAGE_DECISION_LOG_PATH": str(isolated_logs / "AUTOMATED_DECISION_LOG.md"),
            # No optional external intelligence or market-data websocket is
            # needed for this startup probe. Historical public REST requests
            # may still fill a stale cache; credentials are always blanked.
            "INTEL_ENABLED": "false",
            "CRYPTO_BOX_ENABLED": "false",
            "SELF_TUNING_ENABLED": "false",
            "BINANCE_ORDERBOOK_STREAM_ENABLED": "false",
            "OKX_ORDERBOOK_STREAM_ENABLED": "false",
        }
    )
    return {
        "database_snapshot": copied_database,
        "model_artifacts_snapshot": source_models.is_dir(),
        "storage_backend": "sqlite",
        "isolated": True,
    }


def _stop(proc: subprocess.Popen[str], timeout_s: float) -> tuple[int | None, float, bool]:
    """SIGINT once (uvicorn's graceful path); escalate only if it overruns."""
    started = time.monotonic()
    forced = False
    if proc.poll() is None:
        # Popen uses start_new_session=True, so its PID is the process-group
        # ID. Interrupt the whole isolated API tree, not only its root; the
        # latter can leave a spawned worker holding resources after this
        # controller exits.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            forced = True
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
    return proc.returncode, round(time.monotonic() - started, 1), forced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=None, help="sets API_PORT for the run")
    parser.add_argument("--timeout", type=float, default=1800.0, help="seconds to become ready")
    parser.add_argument("--tick-timeout", type=float, default=150.0, help="seconds after ready")
    parser.add_argument("--shutdown-timeout", type=float, default=60.0)
    parser.add_argument("--require-tick", action="append", default=None, help="timeframe")
    # A dashboard configured with the same key can then watch the run.
    parser.add_argument("--api-key-env", default=None, help="read the API key from this variable")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    # Configuration files are resolved relative to the repository, not the
    # caller's directory. The subprocess is isolated even when launched from
    # a different working directory.
    os.chdir(REPO_ROOT)
    command = [c for c in args.command if c != "--"] or [sys.executable, "-m", "src.api"]
    try:
        port = args.port if args.port is not None else _configured_port()
        critical_mb = _critical_rss_mb()
    except Exception as exc:  # the preflight cannot run, which is not a "no"
        print(json.dumps({"preflight": "error", "error": str(exc)[:200]}))
        return 2
    if _listening(port):
        print(json.dumps({"preflight": "error", "error": f"port {port} is already in use"}))
        return 2
    required_ticks = args.require_tick or ["1m"]

    api_key = os.environ.get(args.api_key_env or "", "") or secrets.token_hex(32)
    env = dict(os.environ)
    env.update(
        {
            "TRADING_MODE": "paper",
            "API_PORT": str(port),
            "API_SECRET_KEY": api_key,
            "OPERATOR_SECRET": secrets.token_hex(32),
            "LOG_AS_JSON": "true",
            "PYTHONUNBUFFERED": "1",
        }
    )
    # Never inherit exchange or optional service credentials into the child.
    env.update({name: "" for name in CREDENTIAL_ENV})
    env.update(
        {
            "INTELLIGENCE_GLASSNODE_API_KEY": "",
            "INTELLIGENCE_CRYPTOQUANT_API_KEY": "",
            "MONGODB_URI": "",
        }
    )
    temp_root = Path(tempfile.mkdtemp(prefix="trade-bot-paper-preflight-"))
    atexit.register(shutil.rmtree, temp_root, ignore_errors=True)
    try:
        storage_evidence = _isolate_storage(env, temp_root)
    except Exception as exc:
        print(json.dumps({"preflight": "error", "error": str(exc)[:200]}))
        return 2

    started = time.monotonic()
    proc = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    watcher = _LogWatcher(proc.stdout, started)
    watcher.thread.start()

    def elapsed() -> float:
        return round(time.monotonic() - started, 1)

    try:
        source_commit_sha = _source_commit_sha()
        tracked_worktree_dirty = _tracked_worktree_dirty()
        model_resource_profile = _model_resource_profile()
        from src.config import get_settings

        configured_host = str(get_settings().api.host)
    except Exception as exc:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=args.shutdown_timeout)
        print(
            json.dumps(
                {"preflight": "error", "error": f"cannot identify tested source: {exc}"[:200]}
            )
        )
        return 2
    result: dict[str, Any] = {
        "preflight": "fail",
        "port": port,
        "configured_host": configured_host,
        "command": command[-3:],
        "source_commit_sha": source_commit_sha,
        "tracked_worktree_dirty": tracked_worktree_dirty,
        "configuration_profile": "isolated-paper-sqlite; exchange and optional service credentials blanked; order-book streams disabled; public REST market-data endpoints allowed; 1 GiB XGBoost resource cap applied without modifying .env",
        "model_resource_profile": model_resource_profile,
        "trading_mode": "paper",
        "exchange_credentials_inherited": False,
        "live_orders_enabled": False,
    }
    phases: list[str] = []
    peak_mb = rss_at_ready_mb = rss_mb = 0.0
    memory_limit_exceeded = False
    readiness_failed = False
    listening_s = live_s = api_ready_s = runtime_ready_s = model_ready_s = ready_s = None
    readiness_endpoint_missing = False
    ready_report: Any = None
    deadline = started + args.timeout
    interrupted = False
    try:
        while time.monotonic() < deadline and proc.poll() is None:
            rss_mb = _tree_rss_mb(proc.pid)
            peak_mb = max(peak_mb, rss_mb)
            if rss_mb > critical_mb:
                memory_limit_exceeded = True
                break
            if listening_s is None and _listening(port):
                listening_s = elapsed()
            if listening_s is not None:
                if live_s is None and _get(port, "/livez", api_key)[0] == 200:
                    live_s = elapsed()
                code, body = _get(port, "/readyz", api_key)
                if code == 404:
                    # /status cannot prove that the runtime platform reconciled
                    # operator intent or that each model/loop is healthy. Do not
                    # silently downgrade a required preflight to that weaker API.
                    readiness_endpoint_missing = True
                    break
                if isinstance(body, dict):
                    phase = str(body.get("phase", ""))
                    if phase and (not phases or phases[-1] != phase):
                        phases.append(phase)
                    signals = _readiness_signals(body)
                    if api_ready_s is None and signals["api_ready"]:
                        api_ready_s = elapsed()
                    if runtime_ready_s is None and signals["runtime_ready"]:
                        runtime_ready_s = elapsed()
                    if model_ready_s is None and signals["models_ready"]:
                        model_ready_s = elapsed()
                    if phase == "failed":
                        readiness_failed = True
                        break
                    if ready_s is None and signals["ready"]:
                        ready_s = elapsed()
                        rss_at_ready_mb = rss_mb
                        deadline = time.monotonic() + args.tick_timeout
                    ready_report = body
            if ready_s is not None and all(tf in watcher.first_tick for tf in required_ticks):
                break
            time.sleep(1.0)

    except KeyboardInterrupt:
        interrupted = True
    finally:
        # Even if the preflight controller receives SIGINT (for example, the
        # remote terminal session expires), stop the API process group before
        # returning. Never orphan a training thread/process or isolated DB.
        exit_code, shutdown_s, forced = _stop(proc, args.shutdown_timeout)
        watcher.thread.join(timeout=5)
    logs = watcher.snapshot()
    ticked = all(tf in logs["first_tick_s"] for tf in required_ticks)
    clean = exit_code == 0 and not forced and "api.shutdown_complete" in logs["milestones_s"]
    ok = (
        ready_s is not None
        and ticked
        and not interrupted
        and not readiness_endpoint_missing
        and not memory_limit_exceeded
        and peak_mb <= critical_mb
        and clean
    )
    result.update(
        {
            "preflight": "pass" if ok else "fail",
            "listening_s": listening_s,
            "live_s": live_s,
            "api_ready_s": api_ready_s,
            "runtime_ready_s": runtime_ready_s,
            "model_ready_s": model_ready_s,
            "ready_s": ready_s,
            "readiness_endpoint_missing": readiness_endpoint_missing,
            "phases": phases,
            "state": _summarise(ready_report),
            "required_timeframes": required_ticks,
            "missing_required_timeframes": [
                tf for tf in required_ticks if tf not in logs["first_tick_s"]
            ],
            "first_tick_s": logs["first_tick_s"],
            "rss_mb": {"peak": peak_mb, "at_ready": rss_at_ready_mb, "last": rss_mb},
            "critical_rss_mb": critical_mb,
            "memory_limit_exceeded": memory_limit_exceeded,
            "readiness_failed": readiness_failed,
            "interrupted": interrupted,
            "elapsed_s": elapsed(),
            "shutdown": {"exit_code": exit_code, "seconds": shutdown_s, "forced": forced},
            "milestones_s": logs["milestones_s"],
            "facts": logs["facts"],
            "errors": logs["errors"],
            "storage": storage_evidence,
            "error_lines": logs["log_levels"].get("error", 0),
            "critical_lines": logs["log_levels"].get("critical", 0),
        }
    )
    print(json.dumps(result, separators=(",", ":")))
    return 0 if ok else 1


def _readiness_signals(report: Any) -> dict[str, bool]:
    """Extract independently evidenced startup milestones from /readyz.

    In particular, a global ``warming_up`` phase is not API readiness: the
    API may be serving /livez and /readyz while models are still training.
    A missing component report never counts as a verified runtime/model state.
    """
    if not isinstance(report, dict):
        return {"api_ready": False, "runtime_ready": False, "models_ready": False, "ready": False}
    components = report.get("components")
    if not isinstance(components, dict):
        return {"api_ready": False, "runtime_ready": False, "models_ready": False, "ready": False}
    api = components.get("api")
    runtime = components.get("runtime_platform")
    models = components.get("models")
    loops = components.get("trading_loops")
    api_ready = isinstance(api, dict) and api.get("state") == "ready"
    runtime_ready = (
        isinstance(runtime, dict)
        and runtime.get("state") == "ready"
        and isinstance(runtime.get("reconcile_passes"), int)
        and runtime["reconcile_passes"] >= 1
    )
    timeframe_models = models.get("timeframes") if isinstance(models, dict) else None
    timeframe_loops = loops.get("timeframes") if isinstance(loops, dict) else None
    models_ready = (
        isinstance(models, dict)
        and models.get("state") == "complete"
        and isinstance(timeframe_models, dict)
        and bool(timeframe_models)
        and all(
            isinstance(v, dict) and v.get("state") == "ready" for v in timeframe_models.values()
        )
        and isinstance(loops, dict)
        and loops.get("state") == "running"
        and isinstance(timeframe_loops, dict)
        and bool(timeframe_loops)
        and all(isinstance(v, dict) and v.get("loop") is True for v in timeframe_loops.values())
    )
    return {
        "api_ready": bool(api_ready),
        "runtime_ready": bool(runtime_ready),
        "models_ready": bool(models_ready),
        "ready": report.get("ready") is True,
    }


def _summarise(report: Any) -> dict[str, Any] | None:
    """The last /readyz report, cut down to what decides readiness."""
    if not isinstance(report, dict) or "components" not in report:
        return None
    components = report["components"]
    runtime = components["runtime_platform"]
    return {
        "api": components["api"]["state"],
        "runtime_platform": runtime["state"],
        "reconcile_passes": runtime.get("reconcile_passes"),
        "models": {tf: m["state"] for tf, m in components["models"]["timeframes"].items()},
        "ticks": {tf: t["ticks"] for tf, t in components["trading_loops"]["timeframes"].items()},
        "reasons": report.get("reasons", [])[:3],
    }


if __name__ == "__main__":
    raise SystemExit(main())
