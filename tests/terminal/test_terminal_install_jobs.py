"""
TERM-007, TERM-008 -- installation that never clobbers, job reports that never block.

The installer writes exactly two files and only ever rewrites or removes a
file carrying its own marker; anything else at those paths is a conflict,
reported and left alone. The generated unit and wrapper are checked for the
two properties that matter in practice: they run this checkout's code no
matter which directory the user is in, and systemd empties the cgroup on
stop. The job reporter is held to its promise that reporting can never
affect the job: it does not raise, does not block, and drops events when the
terminal service is absent or slow.

Decides:
  - TERM-007 -- the host CLI and application jobs share the process registry
  - TERM-008 -- install, start and stop never clobber and never orphan
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.terminal import install
from src.terminal.jobs import JobReporter

from ._support import AsyncClient

PYTHON = Path("/opt/venv with space/bin/python")
REPO = Path("/srv/trade bot%1")


class FakeRunner:
    def __init__(self, failing: tuple[str, ...] = ()) -> None:
        self.calls: list[list[str]] = []
        self._failing = failing

    def __call__(self, argv: list[str]) -> int:
        self.calls.append(argv)
        return 1 if any(word in argv for word in self._failing) else 0


@pytest.fixture
def home(tmp_path) -> dict[str, str]:
    return {"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "config")}


@pytest.fixture
def systemctl(monkeypatch):
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/systemctl")


class TestGeneratedFiles:
    def test_the_wrapper_runs_this_checkout_from_any_directory(self):
        text = install.wrapper_text(PYTHON, REPO)
        assert install.MARKER in text
        # -P keeps a `src` package in the caller's directory from shadowing ours.
        assert "-P -m src.terminal" in text
        assert "'/srv/trade bot%1'" in text and "'/opt/venv with space/bin/python'" in text

    def test_the_unit_runs_the_daemon_and_empties_its_cgroup(self):
        text = install.unit_text(PYTHON, REPO)
        assert install.MARKER in text
        assert "KillMode=mixed" in text
        assert "WorkingDirectory=/srv/trade bot%%1" in text
        assert 'Environment="PYTHONPATH=/srv/trade bot%%1"' in text
        assert "exec \"$$1\" -P -m src.terminal daemon" in text
        assert '"/opt/venv with space/bin/python"' in text

    def test_command_quoting_doubles_dollar_signs_only_on_command_lines(self):
        assert install._systemd_quote('a$b"c', command=True) == '"a$$b\\"c"'
        assert install._systemd_quote("a$b", command=False) == '"a$b"'


class TestInstall:
    def test_a_fresh_install_writes_both_files_and_enables_the_unit(self, home, systemctl):
        runner = FakeRunner()
        report = install.install(python=PYTHON, repo=REPO, env=home, run=runner)
        wrapper = install.bin_dir(home) / "tradebot-term"
        unit = install.unit_dir(home) / install.UNIT_NAME
        assert report.ok and set(report.written) == {str(wrapper), str(unit)}
        assert (wrapper.stat().st_mode & 0o777) == 0o755
        assert ["systemctl", "--user", "enable", "--now", install.UNIT_NAME] in runner.calls
        assert any("unit updated" in note for note in report.notes)

    def test_reinstalling_is_an_idempotent_upgrade(self, home, systemctl):
        install.install(python=PYTHON, repo=REPO, env=home, run=FakeRunner())
        report = install.install(python=PYTHON, repo=REPO, env=home, run=FakeRunner())
        assert report.ok and report.written == [] and len(report.unchanged) == 2
        newer = Path("/new/python")
        upgraded = install.install(python=newer, repo=REPO, env=home, run=FakeRunner())
        assert len(upgraded.written) == 2

    def test_a_file_the_installer_did_not_write_is_never_overwritten(self, home, systemctl):
        wrapper = install.bin_dir(home) / "tradebot-term"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("#!/bin/sh\necho mine\n", encoding="utf-8")
        unit = install.unit_dir(home) / install.UNIT_NAME
        unit.parent.mkdir(parents=True)
        unit.write_bytes(b"\xff\xfe not text")
        runner = FakeRunner()
        report = install.install(python=PYTHON, repo=REPO, env=home, run=runner)
        assert not report.ok and len(report.conflicts) == 2
        assert wrapper.read_text(encoding="utf-8") == "#!/bin/sh\necho mine\n"
        assert not any("enable" in call for call in runner.calls)

    def test_without_systemd_only_the_wrapper_is_installed(self, home, monkeypatch):
        monkeypatch.setattr(install.shutil, "which", lambda name: None)
        report = install.install(python=PYTHON, repo=REPO, env=home, run=FakeRunner())
        assert report.ok and len(report.written) == 1
        assert "not available" in report.notes[0]
        skipped = install.install(
            python=PYTHON, repo=REPO, env=home, use_systemd=False, run=FakeRunner()
        )
        assert skipped.notes == ["systemd unit skipped (--no-systemd)"]

    def test_a_unit_that_cannot_be_enabled_is_reported(self, home, systemctl):
        run = FakeRunner(failing=("enable",))
        report = install.install(python=PYTHON, repo=REPO, env=home, run=run)
        assert any("could not enable" in note for note in report.notes)

    def test_the_default_home_and_config_locations(self, monkeypatch, tmp_path):
        units = tmp_path / ".config" / "systemd" / "user"
        assert install.unit_dir({"HOME": str(tmp_path)}) == units
        assert install.bin_dir({"HOME": str(tmp_path)}) == tmp_path / ".local" / "bin"


class TestUninstall:
    def test_removes_exactly_what_it_installed(self, home, systemctl, tmp_path):
        install.install(python=PYTHON, repo=REPO, env=home, run=FakeRunner())
        state = tmp_path / "state"
        state.mkdir()
        (state / "token").write_text("t", encoding="utf-8")
        runner = FakeRunner()
        report = install.uninstall(env=home, state_dir=state, run=runner)
        assert len(report.removed) == 2 and state.exists()
        assert ["systemctl", "--user", "disable", "--now", install.UNIT_NAME] in runner.calls
        purge = install.uninstall(env=home, state_dir=state, purge=True, run=FakeRunner())
        assert purge.removed == [str(state)] and not state.exists()

    def test_foreign_files_are_left_in_place(self, home, tmp_path):
        wrapper = install.bin_dir(home) / "tradebot-term"
        for path in (wrapper, install.unit_dir(home) / install.UNIT_NAME):
            path.parent.mkdir(parents=True)
            path.write_text("someone else's\n", encoding="utf-8")
        report = install.uninstall(env=home, state_dir=tmp_path / "none", run=FakeRunner())
        assert len(report.conflicts) == 2 and report.removed == []

    def test_run_quietly_reports_a_missing_binary_as_127(self):
        assert install.run_quietly(["/nonexistent/binary"]) == 127
        assert install.run_quietly(["true"]) == 0


class TestJobReporter:
    async def test_a_reported_job_appears_in_the_registry(self, daemon):
        watcher = await AsyncClient.connect(daemon.config.socket_path)
        reporter = JobReporter(daemon.config.socket_path)
        try:
            job = reporter.begin("backfill 1h", detail="lookback_days=30")
            reporter.output(job, "bars_written=5\n")
            reporter.end(job, ok=False, error="ValueError: " + "x" * 2000)
            done = await watcher.wait_for(
                lambda f: f["t"] == "process_done" and f["process"]["id"] == job
            )
            assert done["process"]["kind"] == "app"
            assert done["process"]["state"] == "failed"
            assert len(done["process"]["failure_reason"]) == 1024
            await reporter._worker
            assert reporter.sent == 3 and reporter.dropped == 0
        finally:
            await reporter.aclose()
            await watcher.close()

    async def test_reporting_to_a_missing_service_is_dropped_quietly(self, short_tmp):
        reporter = JobReporter(short_tmp / "absent.sock")
        reporter.begin("retrain")
        await reporter._worker
        assert reporter.dropped == 1
        await reporter.aclose()

    async def test_a_refused_event_is_dropped_and_the_next_one_reconnects(self, daemon):
        watcher = await AsyncClient.connect(daemon.config.socket_path)
        reporter = JobReporter(daemon.config.socket_path)
        try:
            reporter.output("p-00000000", "for nobody")  # the daemon answers not_found
            await reporter._worker
            assert reporter.dropped == 1
            job = reporter.begin("after")
            await watcher.wait_for(lambda f: f["t"] == "process" and f["process"]["id"] == job)
        finally:
            await reporter.aclose()
            await watcher.close()

    async def test_a_service_that_never_answers_times_out(self, short_tmp):
        path = short_tmp / "mute.sock"

        async def mute(reader, writer):
            await reader.read()

        server = await asyncio.start_unix_server(mute, path=str(path))
        reporter = JobReporter(path, timeout=0.05)
        try:
            reporter.begin("silent")
            await reporter._worker
            assert reporter.dropped == 1
        finally:
            await reporter.aclose()
            server.close()
            await server.wait_closed()

    async def test_a_full_queue_drops_instead_of_blocking(self, short_tmp):
        reporter = JobReporter(short_tmp / "absent.sock", queue_size=1)
        job = reporter.begin("one")
        reporter.output(job, "two")
        reporter.end(job, ok=True)
        assert reporter.dropped == 2
        await reporter.aclose()

    def test_outside_an_event_loop_nothing_is_sent(self, tmp_path):
        reporter = JobReporter(tmp_path / "x.sock")
        reporter.begin("sync caller")
        assert reporter.dropped == 1

    def test_without_a_socket_reporting_is_disabled(self):
        reporter = JobReporter(None)
        reporter.end(reporter.begin("x"), ok=True)
        assert (reporter.sent, reporter.dropped) == (0, 0)
