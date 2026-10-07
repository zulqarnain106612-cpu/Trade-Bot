"""
The on-disk task store: manifests, the active-task pointer and the branch ledger.

Where: ``$(git rev-parse --git-common-dir)/agent-control/`` unless
``TB_AGENT_STATE_DIR`` overrides it. The common Git dir is the one place that
is (a) shared by every worktree of the clone, so work in another worktree is
never hidden from the task that owns it; (b) outside every work tree, so
recording state can never itself make ``git status`` dirty -- the first
thing the completion validator checks; and (c) never committed or pushed.

How: every read-modify-write is done inside ``locked()`` (an exclusive
``flock`` on ``.lock``) and every write is temp file + fsync + ``os.replace``,
so two hooks firing at once, or a process killed mid-write, leave either the
old manifest or the new one -- never half of each. ``locked()`` is not
re-entrant: callers take it once around the whole operation.

Registry: GOV-060 (config/quality_registry.json).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from src.agent_control.model import (
    SCHEMA_VERSION,
    TASK_ID_RE,
    BranchLedger,
    ManifestError,
    TaskRun,
    load_ledger,
    load_task,
    next_id,
    task_problems,
    to_dict,
)

STATE_DIR_ENV = "TB_AGENT_STATE_DIR"
STATE_DIR_NAME = "agent-control"


class TaskNotFoundError(ManifestError):
    """No manifest exists for the requested task id."""


def default_state_dir(git_common_dir: str | Path) -> Path:
    override = os.environ.get(STATE_DIR_ENV, "").strip()
    return Path(override) if override else Path(git_common_dir) / STATE_DIR_NAME


class TaskStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @staticmethod
    def _read_json(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestError(f"{path.name} is not valid JSON: {exc}") from exc

    def manifest_path(self, task_id: str) -> Path:
        # The id becomes a file name: anything but the exact id shape could
        # walk out of the tasks directory.
        if not TASK_ID_RE.match(task_id):
            raise ManifestError(f"{task_id!r} is not a task id (TB-RUN-YYYYMMDD-NNNN)")
        return self.tasks_dir / f"{task_id}.json"

    def save(self, task: TaskRun) -> None:
        problems = task_problems(task)
        if problems:
            raise ManifestError(f"refusing to save {task.task_id}: " + "; ".join(problems))
        self._write_json(self.manifest_path(task.task_id), to_dict(task))

    def load(self, task_id: str) -> TaskRun:
        path = self.manifest_path(task_id)
        if not path.exists():
            raise TaskNotFoundError(f"no task {task_id} in {self.tasks_dir}")
        return load_task(self._read_json(path))

    def task_ids(self) -> list[str]:
        if not self.tasks_dir.is_dir():
            return []
        stems = (path.stem for path in self.tasks_dir.glob("TB-RUN-*.json"))
        return sorted(stem for stem in stems if TASK_ID_RE.match(stem))

    def next_task_id(self, day: date) -> str:
        return next_id(f"TB-RUN-{day:%Y%m%d}", self.task_ids())

    def _active_map(self) -> dict[str, str]:
        path = self.root / "active.json"
        if not path.exists():
            return {}
        data = self._read_json(path)
        if not isinstance(data, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in data.items()
        ):
            raise ManifestError("active.json must map worktree paths to task ids")
        return data

    def active_task_id(self, worktree: str) -> str | None:
        return self._active_map().get(worktree)

    def set_active(self, worktree: str, task_id: str | None) -> None:
        mapping = self._active_map()
        if task_id is None:
            mapping.pop(worktree, None)
        else:
            mapping[worktree] = task_id
        self._write_json(self.root / "active.json", mapping)

    def load_ledger(self) -> BranchLedger:
        path = self.root / "branch-audit.json"
        if not path.exists():
            return BranchLedger(schema_version=SCHEMA_VERSION)
        return load_ledger(self._read_json(path))

    def save_ledger(self, ledger: BranchLedger) -> None:
        self._write_json(self.root / "branch-audit.json", to_dict(ledger))
