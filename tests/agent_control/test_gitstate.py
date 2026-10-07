"""
GOV-061 -- read-only Git state collection.

What these tests would catch:

* a status parser that loses a path -- one with spaces, a rename's source, an
  unmerged entry -- so a dirty tree is reported clean;
* uncommitted work in a *second* worktree going unseen, which is exactly the
  hidden-WIP failure this layer exists to prevent;
* a merge or rebase left in progress not being reported;
* a git failure or unparsable output silently treated as "nothing there".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agent_control import gitstate
from src.agent_control.gitstate import (
    ACTIVE_OPERATION_PATHS,
    GitError,
    GitResult,
    GitRunner,
    active_operations,
    branch_fields,
    collect_snapshot,
    commit_info,
    commit_paths,
    is_ancestor,
    parse_status_v2,
    parse_worktrees,
    range_paths,
    ref_sha,
    remote_head,
)

STATUS = "\0".join(
    [
        "# branch.oid " + "a" * 40,
        "# branch.head feature",
        "# branch.upstream origin/feature",
        "# branch.ab +2 -1",
        "1 .M N... 100644 100644 100644 h1 h2 path with space.txt",
        "2 R. N... 100644 100644 100644 h1 h2 R100 new name.txt",
        "old name.txt",
        "u UU N... 100644 100644 100644 100644 h1 h2 h3 conflict.txt",
        "? new.txt",
        "! ignored.txt",
        "",
    ]
)


class TestParsing:
    def test_every_record_kind_keeps_its_full_path(self) -> None:
        headers, entries = parse_status_v2(STATUS)
        assert [(e.kind, e.xy, e.path, e.orig_path) for e in entries] == [
            ("changed", ".M", "path with space.txt", None),
            ("renamed", "R.", "new name.txt", "old name.txt"),
            ("unmerged", "UU", "conflict.txt", None),
            ("untracked", "??", "new.txt", None),
            ("ignored", "!!", "ignored.txt", None),
        ]
        fields = branch_fields(headers)
        assert fields.head_sha == "a" * 40
        assert (fields.branch, fields.upstream) == ("feature", "origin/feature")
        assert (fields.ahead, fields.behind) == (2, 1)

    def test_unborn_and_detached_heads_have_no_sha_or_branch(self) -> None:
        fields = branch_fields({"branch.oid": "(initial)", "branch.head": "(detached)"})
        assert fields.head_sha is None and fields.branch is None
        assert fields.ahead is None and fields.behind is None

    def test_a_rename_without_its_source_is_refused(self) -> None:
        with pytest.raises(GitError, match="without its source"):
            parse_status_v2("2 R. N... 100644 100644 100644 h1 h2 R100 new.txt\0")

    def test_an_unknown_record_is_refused(self) -> None:
        with pytest.raises(GitError, match="unrecognised porcelain"):
            parse_status_v2("Z what\0")

    def test_worktree_blocks(self) -> None:
        text = "\n".join(
            [
                "worktree /main",
                "HEAD " + "a" * 40,
                "branch refs/heads/main",
                "",
                "worktree /other",
                "HEAD " + "b" * 40,
                "detached",
                "locked reason",
                "prunable gitdir points nowhere",
                "future-attribute value",
                "",
                "worktree /bare",
                "bare",
            ]
        )
        main, other, bare = parse_worktrees(text)
        assert (main.path, main.branch, main.detached) == ("/main", "main", False)
        assert other.detached and other.locked and other.prunable
        assert other.branch is None
        assert bare.bare

    def test_an_attribute_before_any_worktree_is_refused(self) -> None:
        with pytest.raises(GitError, match="before any 'worktree' line"):
            parse_worktrees("HEAD abc\n")


class TestActiveOperations:
    def _git(self, fake_git, tmp_path: Path, paths: list[str]):
        args: list[str] = ["rev-parse"]
        for name, _ in ACTIVE_OPERATION_PATHS:
            args += ["--git-path", name]
        return fake_git({tuple(args): "\n".join(paths) + "\n"}, cwd=tmp_path)

    def test_leftover_state_files_are_reported_once_each(self, fake_git, tmp_path: Path) -> None:
        (tmp_path / "MERGE_HEAD").write_text("x", encoding="utf-8")
        (tmp_path / "rebase-merge").mkdir()
        (tmp_path / "rebase-apply").mkdir()
        names = [name for name, _ in ACTIVE_OPERATION_PATHS]
        git = self._git(fake_git, tmp_path, [str(tmp_path / names[0]), *names[1:]])
        assert active_operations(git) == ["rebase", "merge"]

    def test_a_short_answer_is_an_error(self, fake_git, tmp_path: Path) -> None:
        with pytest.raises(GitError, match="returned 1 paths"):
            active_operations(self._git(fake_git, tmp_path, ["MERGE_HEAD"]))


class TestSnapshot:
    def test_staged_unstaged_untracked_and_other_worktrees(self, repo, tmp_path: Path) -> None:
        repo.commit("tracked.txt", "one\n")
        repo.write("tracked.txt", "two\n")
        repo.write("staged.txt", "s\n")
        repo.git.run("add", "staged.txt")
        repo.write("untracked dir/new.txt", "n\n")
        other = tmp_path / "other"
        repo.git.run("worktree", "add", "-q", "-b", "side", str(other))
        (other / "side.txt").write_text("side\n", encoding="utf-8")
        gone = tmp_path / "gone"
        repo.git.run("worktree", "add", "-q", "-b", "gone", str(gone))
        (gone / ".git").unlink()

        snapshot = collect_snapshot(repo.git)

        assert snapshot.repository_root == str(repo.path)
        assert snapshot.branch == "feature"
        assert snapshot.head_sha == repo.head()
        assert snapshot.staged_paths == ["staged.txt"]
        expected = ["staged.txt", "tracked.txt", "untracked dir/new.txt"]
        assert sorted(snapshot.dirty_paths) == expected
        assert snapshot.untracked_paths == ["untracked dir/new.txt"]
        assert snapshot.active_operations == []
        trees = {Path(t.path).name: t for t in snapshot.worktrees}
        assert trees["other"].dirty_paths == ["side.txt"]
        assert trees["gone"].prunable and trees["gone"].dirty_paths is None
        assert sorted(trees["work"].dirty_paths or []) == sorted(snapshot.dirty_paths)

    def test_worktrees_are_listed_but_not_inspected_on_request(self, repo) -> None:
        snapshot = collect_snapshot(repo.git, inspect_worktrees=False)
        assert [t.dirty_paths for t in snapshot.worktrees] == [None]

    def test_an_unreadable_worktree_is_left_uninspected(self, fake_git, tmp_path: Path) -> None:
        git_path_args: list[str] = ["rev-parse"]
        for name, _ in ACTIVE_OPERATION_PATHS:
            git_path_args += ["--git-path", name]
        status_args = ("status", "--porcelain=v2", "-z", "--untracked-files=all")
        git = fake_git(
            {
                ("rev-parse", "--show-toplevel", "--git-common-dir"): "/main\n.git\n",
                (*status_args, "--branch"): "# branch.oid (initial)\0# branch.head main\0",
                ("worktree", "list", "--porcelain"): "worktree /main\n\nworktree /broken\n",
                status_args: GitResult(status_args, 128, "", "fatal"),
                tuple(git_path_args): "\n".join(n for n, _ in ACTIVE_OPERATION_PATHS) + "\n",
            },
            cwd=tmp_path,
        )
        snapshot = collect_snapshot(git)
        assert [t.dirty_paths for t in snapshot.worktrees] == [[], None]
        assert snapshot.head_sha is None

    def test_a_repository_that_cannot_locate_itself_is_an_error(self, fake_git, tmp_path) -> None:
        git = fake_git({("rev-parse", "--show-toplevel", "--git-common-dir"): "/only\n"}, tmp_path)
        with pytest.raises(GitError, match="top level and a common dir"):
            collect_snapshot(git)


class TestCommits:
    def test_commit_info_and_paths(self, repo) -> None:
        root = repo.git.run("rev-list", "--max-parents=0", "HEAD").stdout.strip()
        head = repo.commit("a.txt", "a\n", "add a")
        info = commit_info(repo.git)
        assert (info.sha, info.parents, info.subject) == (head, (root,), "add a")
        assert commit_paths(repo.git, info) == ["a.txt"]
        assert commit_paths(repo.git, commit_info(repo.git, root)) == ["README.md"]
        assert range_paths(repo.git, root, head) == ["a.txt"]

    def test_an_unreadable_commit_is_an_error(self, fake_git, tmp_path: Path) -> None:
        git = fake_git({("log", "-1", "--format=%H%x00%P%x00%s", "HEAD"): "garbage\n"}, tmp_path)
        with pytest.raises(GitError, match="could not read commit"):
            commit_info(git)

    def test_ancestry(self, repo) -> None:
        base = repo.head()
        head = repo.commit("a.txt")
        assert is_ancestor(repo.git, base, head)
        assert not is_ancestor(repo.git, head, base)
        with pytest.raises(GitError, match="is-ancestor failed"):
            is_ancestor(repo.git, "0" * 40, head)

    def test_refs_and_remote_heads(self, repo) -> None:
        assert ref_sha(repo.git, "HEAD") == repo.head()
        assert ref_sha(repo.git, "no-such-ref") is None
        assert remote_head(repo.git, "origin", "main") == ref_sha(repo.git, "main")
        assert remote_head(repo.git, "origin", "feature") is None


class TestRunner:
    def test_a_failing_command_raises_with_its_stderr(self, tmp_path: Path) -> None:
        with pytest.raises(GitError, match="exited 128"):
            GitRunner(tmp_path).run("rev-parse", "HEAD")

    def test_the_same_failure_is_returned_when_unchecked(self, tmp_path: Path) -> None:
        assert GitRunner(tmp_path).run("rev-parse", "HEAD", check=False).returncode == 128

    def test_a_process_that_cannot_start_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(GitError, match="git status"):
            GitRunner(tmp_path / "missing").run("status")

    def test_the_environment_disables_optional_locks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, object] = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs["env"])
            seen["input"] = kwargs["input"]

            class Done:
                returncode = 0
                stdout = "ok"
                stderr = ""

            return Done()

        monkeypatch.setattr(gitstate.subprocess, "run", fake_run)
        result = GitRunner(tmp_path).run("status", input_text="in")
        assert result == GitResult(("status",), 0, "ok", "")
        assert seen["GIT_OPTIONAL_LOCKS"] == "0" and seen["LC_ALL"] == "C"
        assert seen["input"] == "in"
