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

import structlog

from src.config import Settings, get_settings

__all__ = ["configure_logging"]


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
