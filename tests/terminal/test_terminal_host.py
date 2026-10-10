"""
TERM-002, TERM-004, SEC-0010 -- /proc readers, configuration bounds and access control.

What the process center shows comes from /proc, parsed here against a
fixture tree (including a comm with spaces and parentheses, which is what
breaks naive parsers). The configuration refuses every value that would make
the terminal reachable from the network or unsafe to use; the token and the
private directories are what keep another local user, or a web page, out.

Decides:
  - TERM-002 -- process identity and exit status are observed, not invented
  - TERM-004 -- the terminal is local-only and authenticated
  - SEC-0010 -- no path hands a shell to anyone but the local user who owns it
"""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

import pytest

from src.terminal import auth, procfs
from src.terminal.config import (
    DEFAULT_ALLOWED_ORIGINS,
    DEFAULT_WS_PORT,
    REPO_ROOT,
    TerminalConfig,
    TerminalConfigError,
    ensure_private_dir,
    validate_loopback_host,
)
from src.terminal.shell_integration import RCFILE, shell_argv, write_rcfile

STAT_LINE = (
    "123 (my (weird) proc) S 1 123 120 0 -1 4194304 100 0 0 0 7 3 0 0 20 0 1 0 "
    "5000 1000000 250 18446744073709551615 1 1 0 0 0 0 0 0 0 0 0 0 17 0 0 0\n"
)


@pytest.fixture
def proc_tree(tmp_path) -> Path:
    root = tmp_path / "proc"
    rows = ((123, 123, 120, 7, 250), (124, 123, 120, 5, 50), (130, 130, 999, 1, 10))
    for pid, pgrp, session, utime, rss in rows:
        directory = root / str(pid)
        directory.mkdir(parents=True)
        line = STAT_LINE.replace("123 (", f"{pid} (", 1)
        fields = line.rsplit(") ", 1)
        rest = fields[1].split()
        rest[2], rest[3], rest[11], rest[21] = str(pgrp), str(session), str(utime), str(rss)
        (directory / "stat").write_text(f"{fields[0]}) {' '.join(rest)}\n", encoding="utf-8")
        (directory / "cmdline").write_bytes(b"python3\x00-m\x00pytest\x00")
    (root / "123" / "cwd").symlink_to(tmp_path)
    (root / "self").mkdir()
    (root / "stat").write_text("cpu  1 2 3\nbtime 1700000000\n", encoding="utf-8")
    return root


class TestProcfs:
    def test_a_comm_with_parentheses_parses(self):
        parsed = procfs.parse_stat(STAT_LINE)
        assert parsed is not None
        assert parsed.comm == "my (weird) proc"
        assert (parsed.pid, parsed.ppid, parsed.pgrp, parsed.session) == (123, 1, 123, 120)
        assert (parsed.cpu_ticks, parsed.start_ticks, parsed.rss_pages) == (10, 5000, 250)

    @pytest.mark.parametrize(
        "raw", ["", "garbage", "12 (x) S 1 2", "x (y) S " + " ".join(map(str, range(1, 23)))]
    )
    def test_an_unparseable_line_is_none_not_an_exception(self, raw):
        assert procfs.parse_stat(raw) is None

    def test_readers_against_a_fixture_tree(self, proc_tree, tmp_path):
        assert procfs.read_stat(123, proc_tree).comm == "my (weird) proc"
        assert procfs.read_stat(999, proc_tree) is None
        assert procfs.read_cmdline(123, proc_tree) == ["python3", "-m", "pytest"]
        assert procfs.read_cmdline(999, proc_tree) is None
        assert procfs.read_cwd(123, proc_tree) == str(tmp_path)
        assert procfs.read_cwd(124, proc_tree) is None
        assert procfs.boot_time(proc_tree) == 1700000000.0

    def test_a_kernel_thread_has_no_argv(self, proc_tree):
        (proc_tree / "124" / "cmdline").write_bytes(b"")
        assert procfs.read_cmdline(124, proc_tree) is None

    def test_boot_time_is_none_when_unknowable(self, tmp_path):
        assert procfs.boot_time(tmp_path / "missing") is None
        (tmp_path / "stat").write_text("cpu 1\n", encoding="utf-8")
        assert procfs.boot_time(tmp_path) is None
        (tmp_path / "stat").write_text("btime nope\n", encoding="utf-8")
        assert procfs.boot_time(tmp_path) is None

    def test_group_usage_sums_the_process_group(self, proc_tree):
        stats = procfs.scan(proc_tree)
        assert sorted(s.pid for s in stats) == [123, 124, 130]
        usage = procfs.group_usage(stats, 123)
        assert usage.count == 2
        assert usage.cpu_ticks == 7 + 3 + 5 + 3
        assert usage.rss_bytes == (250 + 50) * procfs.page_size()
        assert procfs.group_usage(stats, 555) is None

    def test_session_members(self, proc_tree):
        assert sorted(procfs.session_members(procfs.scan(proc_tree), 120)) == [123, 124]

    def test_scan_of_a_missing_root_is_empty(self, tmp_path):
        assert procfs.scan(tmp_path / "nothing") == []

    def test_the_real_proc_describes_this_process(self):
        me = procfs.read_stat(os.getpid())
        assert me is not None and me.pid == os.getpid()
        assert procfs.clock_ticks() > 0


class TestConfig:
    def test_xdg_locations(self, tmp_path):
        config = TerminalConfig.from_env(
            {"XDG_RUNTIME_DIR": str(tmp_path / "r"), "XDG_STATE_HOME": str(tmp_path / "s")}
        )
        assert config.runtime_dir == tmp_path / "r" / "tradebot-terminal"
        assert config.state_dir == tmp_path / "s" / "tradebot-terminal"
        assert config.socket_path.name == "termd.sock"
        assert config.lock_path.parent == config.pid_path.parent == config.runtime_dir
        assert config.token_path.parent == config.rcfile_path.parent == config.log_path.parent
        assert config.default_cwd == REPO_ROOT
        assert (config.ws_host, config.ws_port) == ("127.0.0.1", DEFAULT_WS_PORT)
        assert config.allowed_origins == DEFAULT_ALLOWED_ORIGINS

    def test_fallbacks_without_xdg(self, tmp_path):
        config = TerminalConfig.from_env({"HOME": str(tmp_path)})
        assert config.runtime_dir == Path("/tmp") / f"tradebot-terminal-{os.getuid()}"
        assert config.state_dir == tmp_path / ".local/state/tradebot-terminal"

    def test_explicit_overrides_win(self, tmp_path):
        config = TerminalConfig.from_env(
            {
                "TB_TERMINAL_RUNTIME_DIR": str(tmp_path / "rt"),
                "TB_TERMINAL_STATE_DIR": str(tmp_path / "st"),
                "TB_TERMINAL_DEFAULT_CWD": str(tmp_path),
                "TB_TERMINAL_WS_HOST": "::1",
                "TB_TERMINAL_WS_PORT": "0",
                "TB_TERMINAL_ALLOWED_ORIGINS": "http://localhost:3000, http://127.0.0.1:3000",
                "TB_TERMINAL_MAX_SESSIONS": "3",
                "TB_TERMINAL_BUFFER_BYTES": "65536",
                "TB_TERMINAL_HISTORY": "5",
            }
        )
        assert config.runtime_dir == tmp_path / "rt"
        assert config.default_cwd == tmp_path
        assert config.ws_host == "::1"
        # 0 disables the listener rather than binding a random port.
        assert config.ws_port is None
        assert config.allowed_origins == ("http://localhost:3000", "http://127.0.0.1:3000")
        limits = config.limits
        assert (limits.max_sessions, limits.buffer_bytes, limits.history_entries) == (3, 65536, 5)

    @pytest.mark.parametrize(
        "env",
        [
            {"TB_TERMINAL_RUNTIME_DIR": "relative"},
            {"TB_TERMINAL_STATE_DIR": "relative"},
            {"TB_TERMINAL_DEFAULT_CWD": "relative"},
            {"TB_TERMINAL_RUNTIME_DIR": "/" + "x" * 120},
            {"TB_TERMINAL_WS_HOST": "0.0.0.0"},
            {"TB_TERMINAL_WS_HOST": "192.168.1.5"},
            {"TB_TERMINAL_WS_HOST": "localhost"},
            {"TB_TERMINAL_WS_PORT": "http"},
            {"TB_TERMINAL_WS_PORT": "70000"},
            {"TB_TERMINAL_MAX_SESSIONS": "500"},
            {"TB_TERMINAL_ALLOWED_ORIGINS": "*"},
            {"TB_TERMINAL_ALLOWED_ORIGINS": "http://localhost:5173,null"},
        ],
    )
    def test_unsafe_or_invalid_values_are_refused(self, env, tmp_path):
        with pytest.raises(TerminalConfigError):
            TerminalConfig.from_env({"HOME": str(tmp_path), **env})

    def test_loopback_literals(self):
        assert validate_loopback_host("127.0.0.1") == "127.0.0.1"
        assert validate_loopback_host("::1") == "::1"

    def test_from_env_reads_the_process_environment_by_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TB_TERMINAL_RUNTIME_DIR", str(tmp_path / "x"))
        assert TerminalConfig.from_env().runtime_dir == tmp_path / "x"


class TestPrivateDirectories:
    def test_created_private(self, tmp_path):
        target = ensure_private_dir(tmp_path / "a" / "b")
        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_a_widened_directory_we_own_is_narrowed(self, tmp_path):
        target = tmp_path / "open"
        target.mkdir(mode=0o755)
        os.chmod(target, 0o777)
        ensure_private_dir(target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_a_symlink_is_refused(self, tmp_path):
        (tmp_path / "real").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "real")
        with pytest.raises(TerminalConfigError, match="not a real directory"):
            ensure_private_dir(tmp_path / "link")

    def test_a_directory_owned_by_someone_else_is_refused(self, tmp_path, monkeypatch):
        target = tmp_path / "planted"
        target.mkdir()
        real_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
        with pytest.raises(TerminalConfigError, match="owned by uid"):
            ensure_private_dir(target)


class TestToken:
    def test_created_once_private_and_stable(self, tmp_path):
        path = tmp_path / "token"
        first = auth.load_or_create_token(path)
        assert len(first) == 64 and int(first, 16) >= 0
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert auth.load_or_create_token(path) == first

    def test_rotation_replaces_it(self, tmp_path):
        path = tmp_path / "token"
        first = auth.load_or_create_token(path)
        second = auth.rotate_token(path)
        assert second != first
        assert auth.token_accepted(path, second)
        assert not auth.token_accepted(path, first)

    @pytest.mark.parametrize("presented", [None, "", 12, "f" * 64])
    def test_anything_but_the_exact_token_is_refused(self, tmp_path, presented):
        path = tmp_path / "token"
        auth.load_or_create_token(path)
        assert not auth.token_accepted(path, presented)

    def test_a_missing_token_file_refuses_everything(self, tmp_path):
        assert not auth.token_accepted(tmp_path / "absent", "x" * 64)

    def test_a_widened_token_file_is_narrowed_before_use(self, tmp_path):
        path = tmp_path / "token"
        value = auth.load_or_create_token(path)
        os.chmod(path, 0o644)
        assert auth.token_accepted(path, value)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_truncated_or_foreign_token_file_is_not_trusted(self, tmp_path, monkeypatch):
        short = tmp_path / "short"
        short.write_text("abc\n", encoding="utf-8")
        with pytest.raises(auth.TokenFileError):
            auth.load_or_create_token(short)
        (tmp_path / "real").write_text("a" * 64, encoding="utf-8")
        (tmp_path / "link").symlink_to(tmp_path / "real")
        with pytest.raises(auth.TokenFileError):
            auth.load_or_create_token(tmp_path / "link")
        real_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
        with pytest.raises(auth.TokenFileError):
            auth.load_or_create_token(tmp_path / "real")

    def test_peer_credentials_name_this_user(self):
        left, right = socket.socketpair(socket.AF_UNIX)
        try:
            assert auth.peer_uid(left) == os.getuid()
        finally:
            left.close()
            right.close()


class TestShellIntegration:
    def test_the_rcfile_is_private_and_keeps_the_users_bashrc(self, tmp_path):
        path = write_rcfile(tmp_path / "rc.sh")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        text = path.read_text(encoding="utf-8")
        assert text == RCFILE
        # The user's own configuration loads first; the hooks are added after.
        assert text.index('. "$HOME/.bashrc"') < text.index("PROMPT_COMMAND=")
        assert "133;D;" in text and "133;C" in text and "133;E;" in text

    def test_only_bash_is_integrated(self, tmp_path):
        rc = tmp_path / "rc.sh"
        assert shell_argv("/usr/bin/bash", rc) == ["/usr/bin/bash", "--rcfile", str(rc), "-i"]
        assert shell_argv("/usr/bin/zsh", rc) == ["/usr/bin/zsh", "-i"]
