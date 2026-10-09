"""
TERM-001, TERM-002, TERM-006 -- real PTY sessions, observed processes, clean exits.

Everything here runs a real bash (or sh) on a real pseudo-terminal: the
kernel's line discipline is what turns Ctrl+C into SIGINT and Ctrl+D into
EOF, and those are the behaviours under test, so a fake would only test the
fake. Exit statuses come from the shell integration's markers or from
waitpid, and the tests hold them to the exact numbers a shell would print.
Closing a session must leave no process of that session alive -- including
ones started with ``&`` or ``nohup``.

Decides:
  - TERM-001 -- a terminal session is a real PTY with job control
  - TERM-002 -- process identity and exit status are observed, not invented
  - TERM-006 -- closing a session or stopping the service leaves no orphan
"""

from __future__ import annotations

import asyncio
import os
import re
import signal

import pytest

from src.terminal import sessions as sessions_module
from src.terminal.markers import Marker
from src.terminal.protocol import ProtocolError
from src.terminal.sessions import signal_name

from ._support import Harness, alive, shell_env

SIGTERM_STATUS = 128 + signal.SIGTERM


def _code(fn, *args) -> str:
    with pytest.raises(ProtocolError) as exc_info:
        fn(*args)
    return exc_info.value.code


def by_id(entry_id: str):
    return lambda e: e["id"] == entry_id


def by_command(command: str):
    return lambda e: e["command"] == command


def sleeping(entry: dict) -> bool:
    return entry["command"] == "sleep 30" and bool(entry["pgid"])


async def _yield_until(predicate, rounds: int = 2000) -> bool:
    """Scheduler yields only (sleep(0)): for loop callbacks that raise no event."""
    for _ in range(rounds):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


class TestInteractiveShell:
    async def test_a_command_reports_its_exact_exit_status(self, harness):
        session = await harness.shell()
        await harness.type(session.id, "false\r")
        failed = await harness.wait_finished(by_command("false"))
        assert failed["state"] == "failed"
        assert failed["exit_code"] == 1
        assert failed["failure_reason"] == "exited with status 1"

        command = "echo marker-$((6*7))"
        await harness.type(session.id, command + "\r")
        ok = await harness.wait_finished(by_command(command))
        assert ok["state"] == "succeeded"
        assert ok["exit_code"] == 0
        # The command's own output is retained with its history entry.
        assert b"marker-42" in harness.registry.get(ok["id"]).output

    async def test_ctrl_c_interrupts_the_foreground_job_not_the_shell(self, harness):
        session = await harness.shell()
        await harness.type(session.id, "sleep 30\r")
        running = await harness.wait_running(sleeping)
        # Identity comes from the PTY and /proc, not from what was typed.
        assert running["title"] == "sleep 30"
        assert running["pid"] == running["pgid"]
        assert os.getsid(running["pgid"]) == session.pid
        foreground = harness.manager.describe(session.id)["foreground"]
        assert foreground["pgid"] == running["pgid"]

        harness.manager.write(session.id, b"\x03")
        done = await harness.wait_finished(by_id(running["id"]))
        assert done["exit_code"] == 130
        assert done["state"] == "failed"
        assert session.state == "running"
        await harness.changes.until(lambda: session.foreground is None)

    async def test_a_signal_reaches_only_the_foreground_group(self, harness):
        session = await harness.shell()
        await harness.type(session.id, "sleep 30\r")
        running = await harness.wait_running(sleeping)
        group = harness.manager.signal_foreground(session.id, signal.SIGTERM)
        assert group == running["pgid"]
        done = await harness.wait_finished(by_id(running["id"]))
        assert done["exit_code"] == SIGTERM_STATUS
        assert session.state == "running"

    async def test_resource_use_is_sampled_while_a_command_runs(self, harness):
        session = await harness.shell()
        await harness.type(session.id, "sleep 30\r")
        sampled = await harness.wait_running(
            lambda e: sleeping(e) and e["rss_bytes"] and e["cpu_percent"] is not None
        )
        assert sampled["process_count"] >= 1
        assert sampled["cpu_percent"] >= 0.0
        harness.manager.write(session.id, b"\x03")

    async def test_ctrl_d_ends_the_shell_with_its_status(self, harness):
        session = await harness.shell()
        harness.manager.write(session.id, b"\x04")
        await harness.changes.until(lambda: session.finalized)
        described = harness.manager.describe(session.id)
        assert described["state"] == "exited"
        assert described["exit_code"] == 0
        assert _code(harness.manager.write, session.id, b"x") == "session_exited"
        assert _code(harness.manager.resize, session.id, 10, 10) == "session_exited"

    async def test_resizing_changes_what_programs_see(self, harness):
        session = await harness.shell()
        harness.manager.resize(session.id, 40, 100)
        assert harness.manager.describe(session.id)["rows"] == 40
        await harness.type(session.id, "stty size\r")
        await harness.changes.until(lambda: "40 100" in harness.text(session.id))

    async def test_colour_and_unicode_pass_through_byte_for_byte(self, harness):
        session = await harness.shell()
        await harness.type(session.id, "printf '\\033[31m%s\\033[0m\\n' 'é✓日本'\r")
        expected = "\x1b[31mé✓日本\x1b[0m".encode()
        await harness.changes.until(lambda: expected in harness.output.get(session.id, b""))

    async def test_sessions_are_independent(self, harness, config):
        first = await harness.shell()
        second = await harness.shell(name="second")
        await harness.type(first.id, "cd / && echo only-first\r")
        await harness.wait_finished(lambda e: e["session_id"] == first.id)
        assert harness.manager.describe(first.id)["cwd"] == "/"
        assert harness.manager.describe(second.id)["cwd"] == str(config.default_cwd)
        assert "only-first" not in harness.text(second.id)
        assert harness.manager.describe(second.id)["name"] == "second"

    async def test_a_second_start_marker_closes_the_unfinished_command(self, harness):
        session = await harness.shell()
        at = session.buffer.end
        harness.manager._apply_marker(session, Marker("start", at, at))
        first = session.command_entry
        harness.manager._apply_marker(session, Marker("start", at, at))
        assert harness.registry.get(first).state == "unknown"
        harness.manager._apply_marker(session, Marker("end", at, at, status=0))
        assert session.command_entry is None
        # An end marker with nothing open is ignored.
        harness.manager._apply_marker(session, Marker("end", at, at, status=1))


class TestJobs:
    async def test_a_job_reports_its_status_and_output(self, harness):
        argv = ["sh", "-c", "echo out; echo err >&2; exit 3"]
        job = await harness.manager.create(argv=argv)
        done = await harness.wait_finished(by_id(job.job_entry))
        assert (done["kind"], done["state"], done["exit_code"]) == ("job", "failed", 3)
        output = harness.registry.get(job.job_entry).output
        assert b"out" in output and b"err" in output
        described = harness.manager.describe(job.id)
        assert described["argv"] == argv
        assert described["state"] == "exited"
        assert described["exit_code"] == 3
        assert described["kind"] == "job"

    async def test_a_job_killed_by_a_signal_says_so(self, harness):
        job = await harness.manager.create(argv=["sh", "-c", "kill -TERM $$"])
        done = await harness.wait_finished(by_id(job.job_entry))
        assert done["signal"] == "SIGTERM"
        assert done["failure_reason"] == "terminated by SIGTERM"
        assert harness.manager.describe(job.id)["signal"] == "SIGTERM"

    async def test_a_child_holding_the_terminal_does_not_delay_the_result(
        self, harness, monkeypatch
    ):
        monkeypatch.setattr(sessions_module, "FINALIZE_GRACE_S", 0.0)
        argv = ["sh", "-c", "trap '' HUP; sleep 30 & echo BG=$!; exit 4"]
        job = await harness.manager.create(argv=argv)
        done = await harness.wait_finished(by_id(job.job_entry))
        assert done["exit_code"] == 4
        found = await harness.changes.until(lambda: re.search(r"BG=(\d+)", harness.text(job.id)))
        background = int(found.group(1))
        assert alive(background)
        # Closing the exited session still sweeps what is left in it.
        await harness.manager.close(job.id)
        assert not alive(background)

    async def test_rename_reaches_the_session_and_its_job(self, harness):
        job = await harness.manager.create(argv=["sleep", "30"], name="nightly")
        harness.manager.rename(job.id, "renamed")
        assert harness.manager.describe(job.id)["name"] == "renamed"
        assert harness.registry.get(job.job_entry).detail == "renamed"

    async def test_an_unknown_program_is_refused(self, harness):
        with pytest.raises(ProtocolError) as exc_info:
            await harness.manager.create(argv=["/nonexistent/program"])
        assert exc_info.value.code == "spawn_failed"
        assert harness.manager.sessions() == []

    async def test_the_session_limit_counts_running_sessions(self, harness, config):
        for _ in range(config.limits.max_sessions):
            await harness.manager.create(argv=["sleep", "30"])
        with pytest.raises(ProtocolError) as exc_info:
            await harness.manager.create(argv=["sleep", "30"])
        assert exc_info.value.code == "too_many_sessions"
        assert harness.manager.running_count() == config.limits.max_sessions

    async def test_old_exited_sessions_are_pruned(self, harness, monkeypatch):
        monkeypatch.setattr(sessions_module, "MAX_EXITED_SESSIONS", 1)
        first = await harness.manager.create(argv=["true"])
        await harness.changes.until(lambda: first.finalized)
        second = await harness.manager.create(argv=["true"])
        await harness.changes.until(lambda: second.finalized)
        await harness.manager.create(argv=["sleep", "30"])
        remaining = {s.id for s in harness.manager.sessions()}
        assert first.id not in remaining and second.id not in remaining


class TestCleanup:
    async def test_closing_kills_background_and_nohup_children(self, harness):
        session = await harness.shell()
        line = "sleep 300 & echo BG=$!; nohup sleep 301 >/dev/null 2>&1 & echo NH=$!"
        await harness.type(session.id, line + "\r")
        await harness.changes.until(lambda: re.search(r"NH=\d+", harness.text(session.id)))
        pids = [int(p) for p in re.findall(r"(?:BG|NH)=(\d+)", harness.text(session.id))]
        assert all(alive(p) for p in pids)

        closing = harness.manager.close(session.id)
        await asyncio.gather(closing, harness.manager.close(session.id))

        assert not any(alive(p) for p in pids)
        assert ("session_removed", {"id": session.id}) in harness.events
        assert _code(harness.manager.get, session.id) == "not_found"

    async def test_shutdown_closes_every_session(self, harness):
        job = await harness.manager.create(argv=["sleep", "30"])
        await harness.manager.shutdown()
        assert harness.manager.sessions() == []
        assert not alive(job.pid)


class TestProcessControl:
    async def test_kill_targets_its_own_group_and_guards_against_reuse(self, harness, monkeypatch):
        session = await harness.shell()
        await harness.type(session.id, "sleep 30\r")
        running = await harness.wait_running(sleeping)
        entry_id, real_group = running["id"], running["pgid"]
        kill = harness.manager.kill_process

        # The recorded group now belongs to some other session: refused.
        harness.registry.update(entry_id, pgid=os.getpgrp())
        assert _code(kill, entry_id, signal.SIGTERM) == "not_owned"
        # ... or no longer exists at all.
        harness.registry.update(entry_id, pgid=2**31 - 1)
        assert _code(kill, entry_id, signal.SIGTERM) == "not_running"
        harness.registry.update(entry_id, pgid=real_group)

        def refuse(*_args):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "killpg", refuse)
        assert _code(kill, entry_id, signal.SIGTERM) == "signal_failed"
        foreground = harness.manager.signal_foreground
        assert _code(foreground, session.id, signal.SIGINT) == "signal_failed"
        monkeypatch.undo()

        assert kill(entry_id, signal.SIGTERM) == real_group
        done = await harness.wait_finished(by_id(entry_id))
        assert done["exit_code"] == SIGTERM_STATUS
        assert _code(kill, entry_id, signal.SIGTERM) == "not_running"

    async def test_entries_without_a_live_group_are_not_killable(self, harness):
        kill = harness.manager.kill_process
        app = harness.registry.begin(kind="app", title="retrain")
        assert _code(kill, app.id, signal.SIGTERM) == "not_killable"
        orphan = harness.registry.begin(kind="command", title="x", session_id="s-deadbeef", pgid=1)
        assert _code(kill, orphan.id, signal.SIGTERM) == "not_running"


class TestInput:
    async def test_input_the_pty_cannot_take_yet_is_queued_then_flushed(self, harness):
        job = await harness.manager.create(argv=["sleep", "30"])
        calls = []

        def partial(fd, data):
            calls.append(data)
            if len(calls) == 1:
                return 2
            raise BlockingIOError

        harness.manager._write_fd = partial
        harness.manager.write(job.id, b"hello")
        assert bytes(job.pending_input) == b"llo"
        assert job.writer_registered
        harness.manager.write(job.id, b"!!")
        assert bytes(job.pending_input) == b"llo!!"
        harness.manager._flush_input(job)  # still blocked
        assert bytes(job.pending_input) == b"llo!!"

        harness.manager._write_fd = lambda fd, data: len(data)
        assert await _yield_until(lambda: not job.pending_input and not job.writer_registered)

    async def test_a_pty_that_never_reads_is_refused_more_input(self, harness, monkeypatch):
        job = await harness.manager.create(argv=["sleep", "30"])
        monkeypatch.setattr(sessions_module, "MAX_PENDING_INPUT", 4)
        harness.manager._write_fd = lambda fd, data: 0
        assert _code(harness.manager.write, job.id, b"0123456789") == "input_backlog"

    async def test_a_write_error_is_reported_and_a_flush_error_drops_the_backlog(self, harness):
        job = await harness.manager.create(argv=["sleep", "30"])

        def broken(fd, data):
            raise OSError(5, "Input/output error")

        harness.manager._write_fd = broken
        assert _code(harness.manager.write, job.id, b"x") == "write_failed"
        job.pending_input += b"stale"
        harness.manager._flush_input(job)
        assert not job.pending_input

    async def test_a_spurious_wakeup_reads_nothing(self, harness):
        job = await harness.manager.create(argv=["sleep", "30"])
        before = job.buffer.end
        harness.manager._on_readable(job)
        assert job.buffer.end == before


INHERITED = {
    "npm_lifecycle_event": "electron:dev",
    "npm_config_prefix": "/somewhere",
    "INIT_CWD": "/x",
    "ELECTRON_RUN_AS_NODE": "1",
    "KEEP_ME": "yes",
}


class TestEnvironment:
    async def test_the_child_environment(self, config, short_tmp):
        harness = Harness(config, short_tmp, env=shell_env(short_tmp) | INHERITED)
        try:
            job = await harness.manager.create(argv=["env"])
            await harness.changes.until(lambda: job.finalized)
            text = harness.registry.get(job.job_entry).output.decode()
        finally:
            await harness.close()
        assert f"TB_TERMINAL_SESSION_ID={job.id}" in text
        assert "TERM=xterm-256color" in text
        assert "TERM_PROGRAM=tradebot-terminal" in text
        assert "KEEP_ME=yes" in text
        for gone in ("npm_config_prefix", "npm_lifecycle_event", "INIT_CWD", "ELECTRON_RUN"):
            assert gone not in text


class TestWithoutIntegration:
    async def test_a_plain_shell_still_shows_its_foreground_jobs(self, config, short_tmp):
        harness = Harness(config, short_tmp, shell="/bin/sh")
        try:
            session = await harness.manager.create()
            assert session.argv[-1] == "-i"
            await harness.type(session.id, "sleep 0.2; sleep 30\r")
            first = await harness.wait_running(lambda e: e["title"] == "sleep 0.2")
            assert first["command"] is None
            assert first["started_at"] > 0
            second = await harness.wait_running(lambda e: e["title"] == "sleep 30")
            # The first group ended; without the integration its status is
            # unknown, never assumed to be success.
            ended = harness.finished(by_id(first["id"]))
            assert ended["state"] == "unknown" and ended["exit_code"] is None
            harness.manager.write(session.id, b"\x03")
            done = await harness.wait_finished(by_id(second["id"]))
            assert done["state"] == "unknown"
        finally:
            await harness.close()


def test_signal_names():
    assert signal_name(signal.SIGKILL) == "SIGKILL"
    assert signal_name(200) == "SIG200"
