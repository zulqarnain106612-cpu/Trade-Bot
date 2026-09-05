"""Covers src/api/__main__.py -- the ``python -m src.api`` entrypoint.

The module exists so API_HOST/API_PORT/API_RELOAD are actually honoured; the
test asserts exactly that the settings reach uvicorn.run.

main() also calls configure_logging(), which reads log_level and log_as_json
off the same settings object and reconfigures structlog process-wide. Both
tests keep that away from the rest of the suite: the first passes a fake
carrying the two fields, the second patches the call out entirely so a
runpy execution cannot leave the suite's structlog config rewritten.
"""

from __future__ import annotations

import runpy
from types import SimpleNamespace
from unittest.mock import patch

from src.api import __main__ as api_main


def test_main_passes_the_api_settings_through_to_uvicorn():
    settings = SimpleNamespace(
        api=SimpleNamespace(host="127.0.0.1", port=9000, reload=True),
        log_level="INFO",
        log_as_json=False,
    )
    with (
        patch.object(api_main, "get_settings", return_value=settings),
        patch.object(api_main, "configure_logging") as configure,
        patch.object(api_main.uvicorn, "run") as run,
    ):
        api_main.main()

    configure.assert_called_once_with(settings)
    run.assert_called_once_with("src.api.main:app", host="127.0.0.1", port=9000, reload=True)


def test_module_runs_main_when_executed_as_a_script():
    # Patched at the source module: runpy re-executes __main__, so it binds
    # whatever src.logging_setup exposes at that moment.
    with (
        patch("src.logging_setup.configure_logging"),
        patch("uvicorn.run") as run,
    ):
        runpy.run_module("src.api", run_name="__main__")
    assert run.call_count == 1
