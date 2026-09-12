"""Apply the log_level and log_as_json settings to structlog.

Both settings have existed in Settings since the beginning, with a validator
on log_level restricting it to the five standard names -- and nothing read
either of them. structlog was left at its defaults, so LOG_LEVEL=WARNING
still emitted debug lines and LOG_AS_JSON=true still produced the
human-readable console format. check_static_invariants.py lists log_as_json
under `known_decorative` for exactly that reason.

This is the same gap src/api/__main__.py was written to close for API_PORT
and API_RELOAD: a setting an operator can set, documented as configuration,
that changes nothing.

configure_logging() is idempotent, so calling it from more than one
entrypoint is safe.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any, Final

import structlog

from common.command_schema import redact
from src.config import Settings, get_settings

__all__ = ["configure_logging", "redact_event"]


# SECR-001 -- the log is the most common place a secret ends up, because
# logging is what people add when something is going wrong and the fastest way
# to see a value is to print it. The redaction below is defence in depth: the
# primary control is never passing the secret to the logger, and a value in a
# format no pattern describes still gets through. What it does buy is that the
# three shapes which account for nearly every real incident -- a provider
# token, a connection URI with its password, and an `X=secret` env line --
# cannot reach a log shipper even when somebody logs an exception whole.
#
# The pattern list is `common/command_schema.py`'s, not a second copy: one
# list, so a pattern added for shell output protects the log as well.
_SECRET_KEY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "operator_secret",
        "password",
        "passwd",
        "private_key",
        "secret",
        "seed",
        "token",
        "x-api-key",
    }
)

_REDACTED: Final[str] = "[REDACTED]"


def redact_event(_logger: object, _method: str, event_dict: MutableMapping[str, Any]) -> Any:
    """
    structlog processor: redact secrets in every field of an event.

    Two passes, because the two leaks are different. A field *named* like a
    secret is replaced whole regardless of its value -- the name is the
    evidence, and a short value is not proof of innocence. Every other string
    field goes through the shared pattern redactor, which is what catches a
    token embedded in a rendered exception.
    """
    for key in list(event_dict):
        value = event_dict[key]
        if key.lower() in _SECRET_KEY_NAMES:
            event_dict[key] = _REDACTED
            continue
        if isinstance(value, str):
            cleaned, count = redact(value)
            if count:
                event_dict[key] = cleaned
    return event_dict


def _renderer(as_json: bool) -> object:
    """JSON for log shippers, the console renderer for a terminal.

    The console renderer is what structlog uses when unconfigured, so the
    default (log_as_json=False) leaves output looking exactly as it did.
    """
    if as_json:
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer(colors=False)


def configure_logging(settings: Settings | None = None) -> None:
    """Configure structlog from settings. Safe to call more than once."""
    cfg = settings if settings is not None else get_settings()

    structlog.configure(
        processors=[
            # First, and the reason this function is worth having beyond the
            # two settings: contextvars bound with
            # structlog.contextvars.bind_contextvars() are merged into every
            # event logged in that async context. Without it, binding a
            # correlation id for a request or an order does nothing.
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # After format_exc_info, so a rendered traceback is scanned too:
            # an exception string is where a connection URI most often
            # reaches a log. Before the renderer, so the redaction applies
            # whichever output format is configured.
            redact_event,
            _renderer(cfg.log_as_json),
        ],
        # log_level is enforced here rather than by the stdlib root logger:
        # these are structlog loggers, and a filtering bound logger drops the
        # event before the processor chain runs at all.
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[cfg.log_level]
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
