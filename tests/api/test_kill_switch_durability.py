"""
EXEC-005 — the halt is durable, and the ambiguous cases resolve safely.

An operator disables trading, the supervisor restarts the process, and the bot
comes back trading. Nothing failed, nothing was logged as a failure, and the
audit trail still shows the halt -- which is now false. That is the bug this
file exists for, and it is a bug of omission, so the tests are mostly about
the states nobody wrote code for: the file that is missing, the file that is
half-written, the file from a future schema version.

The rule those tests pin down is an asymmetry that is easy to get backwards:

    missing file      -> the configured default (a first start)
    unreadable file   -> MANUAL, the most restrictive mode (a lost state we
                         cannot rule out having said "halted")

Collapsing them either way is wrong. Treat "missing" as restricted and a
fresh deployment will not trade until someone notices; treat "corrupt" as the
default and a filesystem problem silently revokes an operator's halt.
"""

from __future__ import annotations

import json
import os

import pytest

from src.config import ExecutionMode
from src.execution.mode_persistence import (
    DEFAULT_STATE_PATH,
    ENV_PATH_VAR,
    FAIL_CLOSED_MODE,
    clear_execution_mode,
    load_execution_mode,
    save_execution_mode,
    state_path,
)


@pytest.fixture
def state_file(tmp_path):
    return tmp_path / "execution_mode.json"


class TestTheRoundTrip:
    @pytest.mark.parametrize("mode", list(ExecutionMode))
    def test_every_mode_survives_a_restart(self, state_file, mode):
        save_execution_mode(mode, state_file)
        # A fresh read with a *different* default: if the default leaked
        # through, the assertion would pass for the wrong reason.
        other = next(m for m in ExecutionMode if m is not mode)
        assert load_execution_mode(other, state_file) is mode

    def test_the_last_write_wins(self, state_file):
        for mode in ExecutionMode:
            save_execution_mode(mode, state_file)
        assert load_execution_mode(ExecutionMode.RESTRICTED, state_file) is mode

    def test_saving_is_idempotent(self, state_file):
        save_execution_mode(ExecutionMode.RESTRICTED, state_file)
        first = state_file.read_text()
        save_execution_mode(ExecutionMode.RESTRICTED, state_file)
        assert state_file.read_text() == first


class TestTheAmbiguousStates:
    def test_a_missing_file_yields_the_configured_default(self, state_file):
        assert not state_file.exists()
        assert load_execution_mode(ExecutionMode.AUTOMATIC, state_file) is (ExecutionMode.AUTOMATIC)

    @pytest.mark.parametrize(
        "contents",
        [
            "",
            "{",
            "null",
            "[]",
            '"automatic"',
            '{"version": 1}',
            '{"version": 1, "execution_mode": "turbo"}',
            '{"version": 99, "execution_mode": "automatic"}',
            '{"execution_mode": "automatic"}',
        ],
    )
    def test_anything_malformed_yields_the_fail_closed_mode(self, state_file, contents):
        state_file.write_text(contents)
        # Note the default passed in: AUTOMATIC. A corrupt file must not be
        # able to resurrect it.
        assert load_execution_mode(ExecutionMode.AUTOMATIC, state_file) is FAIL_CLOSED_MODE

    def test_an_unreadable_file_yields_the_fail_closed_mode(self, state_file):
        if os.geteuid() == 0:
            pytest.skip("root ignores the permission bits this test relies on")
        save_execution_mode(ExecutionMode.AUTOMATIC, state_file)
        state_file.chmod(0o000)
        try:
            assert load_execution_mode(ExecutionMode.AUTOMATIC, state_file) is FAIL_CLOSED_MODE
        finally:
            state_file.chmod(0o600)

    def test_a_truncated_write_is_never_observed(self, state_file):
        # The atomicity claim, tested at the level it can be: a reader that
        # sees the file at any point sees a complete document. The temp file
        # the writer used is gone, and nothing partial is left behind.
        save_execution_mode(ExecutionMode.RESTRICTED, state_file)
        leftovers = [p.name for p in state_file.parent.iterdir() if p.name.startswith(".")]
        assert leftovers == []
        assert json.loads(state_file.read_text())["execution_mode"] == "restricted"


class TestTheStatePath:
    def test_it_defaults_to_the_data_directory(self, monkeypatch):
        monkeypatch.delenv(ENV_PATH_VAR, raising=False)
        assert state_path() == DEFAULT_STATE_PATH

    def test_the_environment_overrides_it(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_PATH_VAR, str(tmp_path / "elsewhere.json"))
        assert state_path() == tmp_path / "elsewhere.json"

    def test_a_blank_override_is_not_a_path(self, monkeypatch):
        # An empty env var is how a misconfigured deployment writes the state
        # file to the process's working directory, or to "".
        monkeypatch.setenv(ENV_PATH_VAR, "   ")
        assert state_path() == DEFAULT_STATE_PATH

    def test_the_parent_directory_is_created(self, tmp_path):
        nested = tmp_path / "a" / "b" / "execution_mode.json"
        save_execution_mode(ExecutionMode.RESTRICTED, nested)
        assert nested.exists()


class TestClearing:
    def test_clearing_returns_to_the_configured_default(self, state_file):
        save_execution_mode(ExecutionMode.RESTRICTED, state_file)
        clear_execution_mode(state_file)
        assert load_execution_mode(ExecutionMode.AUTOMATIC, state_file) is (ExecutionMode.AUTOMATIC)

    def test_clearing_a_missing_file_is_not_an_error(self, state_file):
        clear_execution_mode(state_file)


class TestTheApiPersistsAndRestores:
    def test_the_mode_endpoint_persists_before_it_audits(self):
        import inspect

        from src.api import main

        source = inspect.getsource(main.set_execution_mode)
        assert "save_execution_mode(new_mode)" in source
        # Persist first: "halted but unaudited" is recoverable, "audited but
        # still trading" is the outcome nobody detects.
        assert source.index("save_execution_mode") < source.index("insert_audit_event")

    def test_startup_restores_the_persisted_mode_before_readiness(self):
        import inspect

        from src.api import main

        source = inspect.getsource(main.lifespan)
        assert "load_execution_mode(" in source
        assert source.index("load_execution_mode(") < source.index("_state.ready = True")
