"""
API-009 — error responses leak neither secrets nor internals.

An error path is where a system says more than it means to, for a structural
reason: the message is written to help whoever is debugging, and the caller is
assumed to be that person. They are not. The three leaks that actually happen,
in order of how often they do:

  * a validation error echoing the input, which reflects an attacker's own
    payload back and sometimes a credential they sent to the wrong field;
  * an unhandled exception rendering a traceback, which names the framework,
    the file layout and the line that broke;
  * a `str(exc)` on a connection error, which contains the internal hostname,
    the port, and in the MongoDB case the entire URI with its password.

`sanitize_detail` is deliberately blunt. It does not redact the offending
substring and return the rest -- a partially-redacted message is an oracle,
and it invites the arms race over whether the pattern list is complete. A
message that matches anything is replaced wholesale, and the real one goes to
the log where it was useful in the first place.
"""

from __future__ import annotations

import re
from typing import Any, Final

GENERIC_ERROR_DETAIL: Final[str] = "Internal error."
GENERIC_VALIDATION_DETAIL: Final[str] = "Request validation failed."

# Longest message a handler may pass through verbatim. A long detail is
# almost always a rendered exception; short ones are the hand-written strings
# the endpoints raise on purpose.
MAX_DETAIL_LEN: Final[int] = 200

_LEAK_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # Tracebacks and the file paths inside them.
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r'File "[^"]+", line \d+'),
    re.compile(r"(?:/home/|/root/|/usr/|/var/|[A-Za-z]:\\\\)\S*"),
    re.compile(r"\bsite-packages\b"),
    # Credentials, by name and by shape. The name pattern requires a *value*
    # after the label: "Invalid operator secret." is a legitimate 401 and
    # must survive, while "secret=hunter2" must not. Matching the bare word
    # would force every honest auth message into the generic bucket, and a
    # control that mangles correct behaviour gets switched off.
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password|passwd|token|credential)s?\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{8,}"),
    # Connection strings: the single richest leak, since they carry host,
    # port and password together.
    re.compile(r"(?i)\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|redis|amqp|mysql)://\S*"),
    # Internal topology.
    re.compile(r"(?i)\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)\b"),
    re.compile(r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    re.compile(r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"),
    re.compile(r"(?i)\b[\w.-]+\.(?:internal|local|cluster\.local)\b"),
    # Python exception rendering that survived a str() somewhere.
    re.compile(r"\b(?:[A-Za-z_]\w*Error|Exception)\(.*\)"),
    re.compile(r"\bobject at 0x[0-9a-fA-F]+"),
)


def leaks(text: str) -> bool:
    """True if *text* matches anything a caller must not be shown."""
    return any(p.search(text) for p in _LEAK_PATTERNS)


def sanitize_detail(detail: object, fallback: str = GENERIC_ERROR_DETAIL) -> str:
    """
    Return a detail string safe to put in a response body.

    Anything that is not a plain string, is over-long, or matches a leak
    pattern is replaced entirely by *fallback*. The replacement is total on
    purpose: see the module docstring.
    """
    if not isinstance(detail, str):
        return fallback
    if not detail.strip():
        return fallback
    if len(detail) > MAX_DETAIL_LEN:
        return fallback
    if leaks(detail):
        return fallback
    return detail


# How deep a structured detail may nest before it is replaced wholesale. The
# endpoints that build one go two levels; anything deeper is not a
# hand-written detail and does not get the benefit of the doubt.
_MAX_DETAIL_DEPTH: Final[int] = 4


def sanitize_structured(detail: object, _depth: int = 0) -> Any:
    """
    Sanitize a detail that is a dict or a list, leaf by leaf.

    Several endpoints answer with a structured detail on purpose -- the
    gauntlet's failed criteria, for instance -- and flattening those to
    "Internal error." would destroy a legitimate part of the API to protect
    against a leak that is not in them. So the *shape* is preserved and every
    string leaf goes through `sanitize_detail`: the container is ours, the
    strings inside it are what could carry an exception's text.
    """
    if _depth > _MAX_DETAIL_DEPTH:
        return GENERIC_ERROR_DETAIL
    if isinstance(detail, str):
        return sanitize_detail(detail)
    if isinstance(detail, bool | int | float) or detail is None:
        return detail
    if isinstance(detail, dict):
        return {
            str(k): sanitize_structured(v, _depth + 1)
            for k, v in detail.items()
            if isinstance(k, str | int | float)
        }
    if isinstance(detail, list | tuple):
        return [sanitize_structured(v, _depth + 1) for v in detail]
    # An arbitrary object reaching a response body renders as its repr, which
    # includes its module path and address.
    return GENERIC_ERROR_DETAIL


def safe_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, str]]:
    """
    Reduce FastAPI's validation errors to field name and error type.

    Pydantic's rendering includes an `input` key holding the value that
    failed, which is how a mistyped API key ends up quoted back in a 422.
    The caller needs to know *which field* and *what kind of* failure, and
    nothing beyond that is theirs.
    """
    safe: list[dict[str, str]] = []
    for err in errors:
        loc = err.get("loc") or ()
        # Field names come from our own models, so they are safe to name; the
        # values that failed never are.
        field = ".".join(str(part) for part in loc if isinstance(part, str | int))
        safe.append({"field": field or "body", "error": str(err.get("type", "invalid"))})
    return safe


def install_error_handlers(app: Any, logger: Any) -> None:
    """
    Register the sanitizing handlers on *app*.

    Imports are local so this module stays importable -- and unit-testable --
    without FastAPI present, which is what lets the pattern list be tested
    for its own sake rather than only through a live client.
    """
    from fastapi import HTTPException, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # The status code is preserved: it is the endpoint's decision and
        # carries no internals. Only the prose is scrubbed.
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": sanitize_structured(exc.detail)},
        )

    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.info("api.validation_failed", path=request.url.path)
        return JSONResponse(
            status_code=422,
            content={
                "detail": GENERIC_VALIDATION_DETAIL,
                "errors": safe_validation_errors(list(exc.errors())),
            },
        )

    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # The full exception, with its traceback, goes to the log -- the only
        # place it was ever useful.
        logger.error(
            "api.unhandled_exception",
            path=request.url.path,
            error=type(exc).__name__,
            exc_info=True,
        )
        return JSONResponse(status_code=500, content={"detail": GENERIC_ERROR_DETAIL})

    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
