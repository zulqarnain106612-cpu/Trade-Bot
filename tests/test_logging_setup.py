"""log_level and log_as_json must actually change logging behaviour.

Both settings shipped with a validator and no consumer: LOG_LEVEL=WARNING
still emitted debug lines and LOG_AS_JSON=true still produced the console
format. check_static_invariants.py listed log_as_json under
`known_decorative` for that reason, and this module is why that entry is
gone.

Each test reconfigures structlog globally, so the fixture restores the
previous configuration afterwards -- otherwise the format chosen by
whichever test ran last would leak into the rest of the suite.
"""

from __future__ import annotations

import json

import pytest
import structlog

from src.config import Settings
from src.logging_setup import configure_logging


@pytest.fixture(autouse=True)
def _restore_structlog_config():
    previous = structlog.get_config()
    try:
        yield
    finally:
        structlog.configure(**previous)


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


def test_log_as_json_true_emits_parseable_json(capsys) -> None:
    configure_logging(_settings(log_as_json=True))

    structlog.get_logger(__name__).info("order_filled", symbol="BTC/USDT", qty=0.5)

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "order_filled"
    assert payload["symbol"] == "BTC/USDT"
    assert payload["qty"] == 0.5
    assert payload["level"] == "info"


def test_log_as_json_false_does_not_emit_json(capsys) -> None:
    configure_logging(_settings(log_as_json=False))

    structlog.get_logger(__name__).info("order_filled", symbol="BTC/USDT")

    out = capsys.readouterr().out
    assert "order_filled" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip())


def test_log_level_suppresses_events_below_it(capsys) -> None:
    configure_logging(_settings(log_level="WARNING", log_as_json=True))
    log = structlog.get_logger(__name__)

    log.info("should_not_appear")
    log.warning("should_appear")

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    events = [json.loads(ln)["event"] for ln in lines]
    assert events == ["should_appear"]


def test_contextvars_are_merged_into_events(capsys) -> None:
    """The reason this module exists beyond the two settings.

    Without merge_contextvars in the chain, bind_contextvars() is silently
    a no-op -- which is what makes a correlation id impossible to add.
    """
    configure_logging(_settings(log_as_json=True))
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(trace_id="abc123")
    try:
        structlog.get_logger(__name__).info("order_submitted")
    finally:
        structlog.contextvars.clear_contextvars()

    assert json.loads(capsys.readouterr().out.strip())["trace_id"] == "abc123"


def test_configure_logging_is_idempotent(capsys) -> None:
    """Two entrypoints call it; the second must not double-emit or throw."""
    configure_logging(_settings(log_as_json=True))
    configure_logging(_settings(log_as_json=True))

    structlog.get_logger(__name__).info("once")

    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1
