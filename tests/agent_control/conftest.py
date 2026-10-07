"""
Shared fixtures for the agent-control suite.

``repo`` is a real Git repository in ``tmp_path`` with a local bare
``origin``, so push and ``ls-remote`` behaviour is exercised without the
network. Git is run through ``GitRunner`` -- the module under test -- rather
than a second process helper. ``fake_git`` serves canned output for the
parsing paths, where a real repository would only slow the test down.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.agent_control.gitstate import GitError, GitResult, GitRunner
from src.agent_control.model import TaskRun, TaskScope, ValidationRequirement
from src.agent_control.workspace import Workspace


@dataclass
class Repo:
    path: Path
    git: GitRunner
    origin: Path

    def write(self, name: str, content: str = "x\n") -> Path:
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def commit(self, name: str, content: str = "x\n", message: str = "change") -> str:
        self.write(name, content)
        self.git.run("add", name)
        self.git.run("commit", "-q", "-m", message)
        return self.head()

    def head(self) -> str:
        return self.git.run("rev-parse", "HEAD").stdout.strip()

    def push(self) -> None:
        self.git.run("push", "-q", "origin", "HEAD")

    def workspace(self) -> Workspace:
        return Workspace.open(self.path)


class FakeGit:
    """Canned ``git`` answers keyed by argv; anything unexpected is a test bug."""

    def __init__(self, answers: Mapping[tuple[str, ...], GitResult | str], cwd: Path) -> None:
        self.answers = dict(answers)
        self.cwd = cwd
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        *args: str,
        check: bool = True,
        cwd: Path | str | None = None,
        input_text: str | None = None,
    ) -> GitResult:
        self.calls.append(args)
        answer = self.answers[args]
        result = answer if isinstance(answer, GitResult) else GitResult(args, 0, answer, "")
        if check and result.returncode != 0:
            raise GitError(f"git {' '.join(args)} exited {result.returncode}")
        return result


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state"
    monkeypatch.setenv("TB_AGENT_STATE_DIR", str(path))
    return path


@pytest.fixture
def repo(tmp_path: Path, state_dir: Path) -> Repo:
    origin = tmp_path / "origin.git"
    GitRunner(tmp_path).run("init", "-q", "--bare", "-b", "main", str(origin))
    path = tmp_path / "work"
    path.mkdir()
    git = GitRunner(path)
    git.run("init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "Agent Control Test"),
        ("user.email", "agent-control@example.invalid"),
        ("commit.gpgsign", "false"),
    ):
        git.run("config", key, value)
    git.run("remote", "add", "origin", str(origin))
    created = Repo(path=path, git=git, origin=origin)
    created.commit("README.md", "base\n", "base")
    git.run("push", "-q", "origin", "main")
    git.run("fetch", "-q", "origin")
    git.run("switch", "-q", "-c", "feature")
    return created


@pytest.fixture
def make_task(repo: Repo) -> Callable[..., TaskRun]:
    """A task on ``repo``: one deliverable (feature.txt), one runnable validation, one step."""

    def make(**scope: bool) -> TaskRun:
        return repo.workspace().create_task(
            "demo objective",
            scope=TaskScope(**scope),
            deliverables=[("the feature file", ["feature.txt"])],
            validations=[ValidationRequirement("smoke", ["true"])],
            steps=["write the feature"],
        )

    return make


@pytest.fixture
def fake_git() -> type[FakeGit]:
    return FakeGit
