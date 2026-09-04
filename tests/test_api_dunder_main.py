"""Covers src/api/__main__.py -- the ``python -m src.api`` entrypoint.

The module exists so API_HOST/API_PORT/API_RELOAD are actually honoured; the
test asserts exactly that the settings reach uvicorn.run.
"""

from __future__ import annotations

import runpy
from types import SimpleNamespace
from unittest.mock import patch

from src.api import __main__ as api_main


def test_main_passes_the_api_settings_through_to_uvicorn():
    # log_level/log_as_json: main() calls configure_logging(settings) before
    # handing off to uvicorn, so the double has to carry them.
    settings = SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=9000, reload=True),
        log_level="INFO",
        log_as_json=False,
    )
    with (
        patch.object(api_main, "get_settings", return_value=settings),
        patch.object(api_main.uvicorn, "run") as run,
    ):
        api_main.main()

    run.assert_called_once_with("src.api.main:app", host="127.0.0.1", port=9000, reload=True)


def test_module_runs_main_when_executed_as_a_script():
    with patch("uvicorn.run") as run:
        runpy.run_module("src.api", run_name="__main__")
    assert run.call_count == 1
