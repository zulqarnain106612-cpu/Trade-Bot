"""Fixtures for the terminal service tests; the helpers are in _support.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.terminal.config import TerminalConfig
from src.terminal.shell_integration import write_rcfile

from ._support import Daemon, Harness, ThreadedDaemon, make_config


@pytest.fixture
def short_tmp(tmp_path_factory) -> Path:
    # sun_path is 107 bytes; a tmp_path that embeds the test name can exceed it.
    return tmp_path_factory.mktemp("t")


@pytest.fixture
def config(short_tmp) -> TerminalConfig:
    return make_config(short_tmp)


@pytest.fixture
async def harness(config, short_tmp):
    write_rcfile(config.rcfile_path)
    h = Harness(config, short_tmp)
    yield h
    await h.close()


@pytest.fixture
async def daemon(config, short_tmp):
    d = await Daemon(config, short_tmp).started()
    yield d
    if not d.task.done():
        await d.shutdown()


@pytest.fixture
def threaded_daemon(config, short_tmp, monkeypatch):
    monkeypatch.setenv("TB_TERMINAL_RUNTIME_DIR", str(config.runtime_dir))
    monkeypatch.setenv("TB_TERMINAL_STATE_DIR", str(config.state_dir))
    monkeypatch.setenv("TB_TERMINAL_WS_PORT", "0")
    monkeypatch.setenv("TB_TERMINAL_DEFAULT_CWD", str(config.default_cwd))
    d = ThreadedDaemon(config, short_tmp).start()
    yield d
    d.shutdown()
