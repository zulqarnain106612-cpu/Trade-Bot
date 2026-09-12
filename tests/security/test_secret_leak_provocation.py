"""
SECR-001 — provoke the leaks rather than assert they are absent.

The requirement says it plainly: *deliberately trigger* errors that involve an
API key, a database password, an authorization header, and check what comes
out. That is a different test from "we don't log secrets", which is an
assertion about intent and passes trivially on the day somebody adds
`log.error("auth failed", key=key)`.

So each test here creates the leak first. A handler is handed a key, a
connection URI, a header, a whole exception object -- and the assertion is on
what survives into the rendered event.

Two things this deliberately does not claim. It is not proof that no secret
can ever reach a log: a value in a format no pattern describes passes
through, and the real control is not handing secrets to the logger at all.
And it does not cover the shell path, which `common/shell_exec.py` redacts
separately using the same pattern list -- one list, tested from both ends.
"""

from __future__ import annotations

import json

import pytest
import structlog

from common.command_schema import redact
from src.logging_setup import configure_logging, redact_event

API_KEY = "sk-ant-abcdefghijklmnopqrstuvwxyz0123456789"
GITHUB_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
MONGO_URI = "mongodb+srv://admin:hunter2@cluster0.example.net/trades"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
PRIVATE_KEY = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqh\n-----END PRIVATE KEY-----"


def render(**fields) -> str:
    """Run one event through the processor and return it as text."""
    return json.dumps(redact_event(None, "info", dict(fields)), default=str)


class TestProvokedLeaksInLogFields:
    @pytest.mark.parametrize("secret", [API_KEY, GITHUB_TOKEN, MONGO_URI, JWT, PRIVATE_KEY])
    def test_a_secret_in_a_message_does_not_survive(self, secret):
        out = render(event=f"upstream call failed: {secret}")
        assert secret not in out

    @pytest.mark.parametrize(
        "field", ["api_key", "token", "password", "secret", "operator_secret", "authorization"]
    )
    def test_a_field_named_like_a_secret_is_replaced_whatever_it_holds(self, field):
        # The name is the evidence. A short value is not proof of innocence --
        # a four-character password is still a password.
        out = render(**{field: "abcd"})
        assert "abcd" not in out
        assert "REDACTED" in out

    def test_the_field_name_survives_so_the_log_is_still_useful(self):
        out = render(api_key="whatever")
        assert "api_key" in out

    def test_an_x_api_key_header_field_is_covered(self):
        out = render(**{"x-api-key": "x" * 32})
        assert "x" * 32 not in out

    def test_unrelated_fields_are_untouched(self):
        # A redactor that mangles ordinary events gets switched off.
        out = render(event="order filled", symbol="BTC/USDT", qty=0.25)
        assert "BTC/USDT" in out
        assert "order filled" in out


class TestProvokedLeaksInExceptions:
    def test_a_connection_error_carrying_a_uri_is_scrubbed(self):
        # The single richest leak in practice: pymongo renders the whole URI,
        # password included, into the exception text.
        try:
            raise ConnectionError(f"could not connect to {MONGO_URI}")
        except ConnectionError as exc:
            out = render(event="storage unavailable", error=str(exc))
        assert "hunter2" not in out

    def test_an_auth_failure_carrying_the_key_is_scrubbed(self):
        try:
            raise PermissionError(f"rejected key {API_KEY}")
        except PermissionError as exc:
            out = render(event="auth failed", error=str(exc))
        assert API_KEY not in out

    def test_an_env_style_assignment_is_scrubbed(self):
        out = render(event=f"startup failed: API_SECRET_KEY={'z' * 32}")
        assert "z" * 32 not in out


class TestTheProcessorIsWiredIn:
    def test_configure_logging_installs_it(self):
        configure_logging()
        processors = structlog.get_config()["processors"]
        assert redact_event in processors

    def test_it_runs_after_the_traceback_is_rendered(self):
        # Before format_exc_info, the exception is still an object and the
        # secret inside it is invisible to a string scan.
        configure_logging()
        names = [
            getattr(p, "__name__", type(p).__name__) for p in structlog.get_config()["processors"]
        ]
        # structlog renders `format_exc_info` as an ExceptionRenderer
        # instance in this version; the name to assert on is the one the
        # configured chain actually reports.
        assert names.index("ExceptionRenderer") < names.index("redact_event")

    def test_it_runs_before_the_renderer(self):
        configure_logging()
        processors = structlog.get_config()["processors"]
        assert processors.index(redact_event) == len(processors) - 2


class TestOnePatternList:
    def test_the_log_redactor_uses_the_shared_patterns(self):
        # Two copies of a pattern list is one copy that gets a new pattern and
        # one that does not.
        import inspect

        from src import logging_setup

        assert "from common.command_schema import redact" in inspect.getsource(logging_setup)

    @pytest.mark.parametrize("secret", [API_KEY, GITHUB_TOKEN, MONGO_URI, JWT])
    def test_the_shared_redactor_catches_it_directly(self, secret):
        cleaned, count = redact(secret)
        assert count >= 1
        assert secret not in cleaned
