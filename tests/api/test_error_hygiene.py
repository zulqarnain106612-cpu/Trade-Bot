"""
API-009 — error responses leak neither secrets nor internals.

Three leaks account for almost every real finding in this class, and each one
is here as a test built from the thing that actually leaks: a rendered
traceback, a connection URI with its password in it, and a validation error
echoing the caller's input. The third is the sneakiest, because the framework
produces it for free and it looks like good API design.

The sanitizer is deliberately all-or-nothing, so the tests are too. A
partially-redacted message is still an oracle -- the caller learns that
something matched -- and defending a redaction routine against every encoding
of a secret is a game with no end state. Replace the whole message; keep the
real one in the log.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from src.api.error_hygiene import (
    GENERIC_ERROR_DETAIL,
    GENERIC_VALIDATION_DETAIL,
    MAX_DETAIL_LEN,
    leaks,
    safe_validation_errors,
    sanitize_detail,
    sanitize_structured,
)

# Messages that have all been seen in the wild, in some form, in a response
# body. Each is a different category, not a variation on one.
LEAKY = [
    'Traceback (most recent call last):\n  File "/home/bot/src/api/main.py", line 42',
    "pymongo.errors.ServerSelectionTimeoutError(mongodb://admin:hunter2@10.0.1.7:27017)",
    "connection refused to redis://cache.internal:6379",
    "could not reach 192.168.1.14:8443",
    "listening on 127.0.0.1:8000",
    "api_key=sk_live_abcd1234efgh5678",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9",
    "password: hunter2",
    "/usr/lib/python3.12/site-packages/httpx/_client.py",
    "<src.execution.live.LiveExecutor object at 0x7f3a91c2b4e6>",
    "ValueError('symbol BTC/USD not in universe')",
]

# Messages the endpoints raise on purpose. If the sanitizer eats these, the
# API stops being usable and somebody switches the sanitizer off -- which is
# the failure mode that matters more than any single missed pattern.
LEGITIMATE = [
    "Invalid operator secret.",
    "OPERATOR_SECRET is not configured on the server.",
    "Rate limit exceeded for resolve_approval (60/min).",
    "Server starting up. Retry shortly.",
    "No such object, or it is not visible to this caller.",
    "Executor not initialized",
    "Invalid request_id format.",
    "A required security control is unavailable; trading endpoints are closed.",
]


class _OperatorBody(BaseModel):
    """Module level, not nested in the fixture: `from __future__ import
    annotations` makes the parameter annotation a string that FastAPI
    resolves against the module namespace, and a class defined inside the
    fixture is not in it -- the body parameter silently becomes a query
    parameter and the test validates the wrong thing."""

    operator: str


class TestTheLeakDetector:
    @pytest.mark.parametrize("message", LEAKY)
    def test_it_catches_what_leaks(self, message):
        assert leaks(message) is True

    @pytest.mark.parametrize("message", LEGITIMATE)
    def test_it_passes_what_does_not(self, message):
        assert leaks(message) is False


class TestSanitizeDetail:
    @pytest.mark.parametrize("message", LEAKY)
    def test_a_leaky_message_is_replaced_entirely(self, message):
        cleaned = sanitize_detail(message)
        assert cleaned == GENERIC_ERROR_DETAIL

    @pytest.mark.parametrize("message", LEAKY)
    def test_no_fragment_of_it_survives(self, message):
        # The all-or-nothing property, stated as a property: nothing longer
        # than a word from the original may appear in the output.
        cleaned = sanitize_detail(message)
        for token in message.split():
            if len(token) > 6:
                assert token not in cleaned

    @pytest.mark.parametrize("message", LEGITIMATE)
    def test_a_legitimate_message_survives_unchanged(self, message):
        assert sanitize_detail(message) == message

    def test_an_over_long_message_is_replaced_whatever_it_says(self):
        # Length alone is evidence: the hand-written details are short, and
        # anything long is a rendered exception the pattern list may not
        # happen to cover.
        assert sanitize_detail("a" * (MAX_DETAIL_LEN + 1)) == GENERIC_ERROR_DETAIL

    @pytest.mark.parametrize("value", [None, 42, {"detail": "x"}, ["x"], b"x", ""])
    def test_non_strings_and_blanks_become_the_generic_message(self, value):
        assert sanitize_detail(value) == GENERIC_ERROR_DETAIL

    def test_the_fallback_is_overridable_for_the_validation_path(self):
        assert sanitize_detail("boom" * 200, GENERIC_VALIDATION_DETAIL) == (
            GENERIC_VALIDATION_DETAIL
        )


class TestStructuredDetails:
    """
    Some endpoints answer with a structured detail deliberately -- the
    gauntlet's failed criteria are the live example. Flattening those would
    break a working part of the API in order to defend against a leak that is
    not in them, and a control that breaks correct behaviour gets removed.
    """

    def test_a_structured_detail_keeps_its_shape(self):
        detail = {"error": "gauntlet_not_passed", "failed_criteria": ["sharpe", "trade_count"]}
        assert sanitize_structured(detail) == detail

    def test_a_leaky_string_leaf_is_replaced_in_place(self):
        detail = {"error": "upstream", "cause": "mongodb://admin:hunter2@10.0.1.7:27017"}
        cleaned = sanitize_structured(detail)
        assert cleaned["error"] == "upstream"
        assert cleaned["cause"] == GENERIC_ERROR_DETAIL

    def test_numbers_and_nulls_pass_through(self):
        assert sanitize_structured({"count": 3, "ratio": 0.5, "prev": None, "ok": True}) == {
            "count": 3,
            "ratio": 0.5,
            "prev": None,
            "ok": True,
        }

    def test_an_arbitrary_object_is_replaced(self):
        # Its repr carries a module path and an address.
        assert sanitize_structured(object()) == GENERIC_ERROR_DETAIL

    def test_deep_nesting_is_replaced_rather_than_walked(self):
        deep: object = "leaf"
        for _ in range(12):
            deep = {"next": deep}
        assert GENERIC_ERROR_DETAIL in str(sanitize_structured(deep))


class TestValidationErrorsDoNotEchoInput:
    def test_the_offending_value_is_dropped(self):
        # Pydantic puts the failing value in `input`. A caller who sent their
        # API key to the wrong field gets it quoted back at them otherwise.
        errors = [
            {
                "loc": ("body", "operator_secret"),
                "msg": "string too short",
                "type": "string_too_short",
                "input": "sk_live_abcd1234efgh5678",
            }
        ]
        safe = safe_validation_errors(errors)
        assert safe == [{"field": "body.operator_secret", "error": "string_too_short"}]
        assert "sk_live_abcd1234efgh5678" not in str(safe)

    def test_the_message_prose_is_dropped_too(self):
        # `msg` is rendered from the input in several pydantic validators.
        errors = [
            {
                "loc": ("body", "symbol"),
                "msg": "value is not a valid symbol: '../../etc/passwd'",
                "type": "value_error",
                "input": "../../etc/passwd",
            }
        ]
        assert "passwd" not in str(safe_validation_errors(errors))

    def test_the_field_name_is_kept_because_the_caller_needs_it(self):
        errors = [{"loc": ("body", "quantity"), "type": "greater_than", "input": -1}]
        assert safe_validation_errors(errors)[0]["field"] == "body.quantity"

    def test_an_indexed_location_renders_readably(self):
        errors = [{"loc": ("body", "orders", 3, "price"), "type": "missing"}]
        assert safe_validation_errors(errors)[0]["field"] == "body.orders.3.price"

    def test_a_missing_location_still_produces_a_field(self):
        assert safe_validation_errors([{"type": "missing"}])[0]["field"] == "body"


class TestTheHandlersAreInstalled:
    def test_the_app_registers_a_handler_for_unhandled_exceptions(self):
        from src.api import main

        # The bare Exception handler is the one that matters: the leaks come
        # from the paths nobody anticipated, by definition.
        assert Exception in main.app.exception_handlers

    def test_the_app_registers_the_validation_handler(self):
        from fastapi.exceptions import RequestValidationError

        from src.api import main

        assert RequestValidationError in main.app.exception_handlers

    def test_the_app_registers_the_http_exception_handler(self):
        from starlette.exceptions import HTTPException as StarletteHTTPException

        from src.api import main

        assert StarletteHTTPException in main.app.exception_handlers


class TestEndToEndThroughTheApp:
    """
    The unit tests prove the sanitizer; these prove a response actually goes
    through it. Built on a throwaway app rather than the real one so the
    failing endpoint can be made to fail on demand.
    """

    @pytest.fixture
    def client(self):
        import structlog
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient

        from src.api.error_hygiene import install_error_handlers

        app = FastAPI()

        @app.get("/boom")
        async def boom() -> dict[str, str]:
            raise RuntimeError(
                'Traceback (most recent call last):\n  File "/home/bot/x.py", line 9'
            )

        @app.get("/leaky-http")
        async def leaky_http() -> dict[str, str]:
            raise HTTPException(
                status_code=502, detail="upstream mongodb://admin:hunter2@10.0.1.7:27017 down"
            )

        @app.get("/honest-http")
        async def honest_http() -> dict[str, str]:
            raise HTTPException(status_code=401, detail="Invalid operator secret.")

        @app.post("/validated")
        async def validated(body: _OperatorBody) -> dict[str, str]:
            return {"operator": body.operator}

        # A structlog logger, not a stdlib one: the handlers log with keyword
        # fields, and a stdlib logger raises on those -- inside the handler,
        # where the failure turns into the bare 500 the handler existed to
        # replace. The first run of this suite found exactly that.
        install_error_handlers(app, structlog.get_logger("test"))
        # raise_server_exceptions=False: the point is what the *client* sees,
        # not what the test runner would re-raise.
        return TestClient(app, raise_server_exceptions=False)

    def test_an_unhandled_exception_returns_a_generic_500(self, client):
        resp = client.get("/boom")
        assert resp.status_code == 500
        assert resp.json() == {"detail": GENERIC_ERROR_DETAIL}
        assert "Traceback" not in resp.text
        assert "/home/bot" not in resp.text

    def test_a_leaky_http_exception_keeps_its_status_and_loses_its_prose(self, client):
        resp = client.get("/leaky-http")
        assert resp.status_code == 502  # the status is the endpoint's decision
        assert "hunter2" not in resp.text
        assert "10.0.1.7" not in resp.text
        assert resp.json()["detail"] == GENERIC_ERROR_DETAIL

    def test_an_honest_http_exception_is_passed_through(self, client):
        resp = client.get("/honest-http")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid operator secret."

    def test_a_validation_failure_does_not_echo_the_body(self, client):
        resp = client.post("/validated", json={"operator": ["sk_live_abcd1234efgh5678"]})
        assert resp.status_code == 422
        assert "sk_live_abcd1234efgh5678" not in resp.text
        assert resp.json()["detail"] == GENERIC_VALIDATION_DETAIL
        assert resp.json()["errors"][0]["field"] == "body.operator"
