"""
API-002 — object references a caller cannot use to reach somebody else's data.

Every identifier this API accepts in a path segment arrives from the caller,
and the two ways that goes wrong are independent:

  1. The identifier is used as a *key* somewhere -- a dict lookup, a storage
     query, a filename. An identifier that is allowed to contain ``/``, ``..``
     or a null byte is a traversal or an injection depending only on which
     lookup it reaches.
  2. The *response* distinguishes "no such order" from "not your order". A
     caller who can tell those apart can enumerate the identifier space and
     learn what exists without ever being allowed to read it.

So this module does two small things and refuses to do a third. It validates
the shape of an identifier before it is used for anything, and it produces one
response for every negative outcome. It does not tell the caller which
identifier failed -- echoing the input back is how a "not found" message
becomes a reflection point.

The single-key deployments this bot runs in have one tenant, which is exactly
why the check lives in a module rather than in a comment: the day a second
principal exists, the scoping is already enforced at the boundary and the
change is a call site, not an audit.
"""

from __future__ import annotations

import re
from typing import Final

# Identifiers this API mints or accepts: exchange order ids, approval ids,
# strategy names. Deliberately narrow -- alphanumeric with internal dashes and
# underscores, never leading punctuation, never a separator of any kind.
#
# The upper bound is 64: every identifier the exchanges return fits well
# inside it, and an unbounded identifier is a memory cost paid before any
# authorization check has run.
_OBJECT_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

MAX_OBJECT_ID_LEN: Final[int] = 64

# One message for "no such object", "aged out of the registry", "belongs to
# another principal" and "never existed". They are indistinguishable on
# purpose: an attacker who can separate them has an oracle.
NOT_FOUND_DETAIL: Final[str] = "No such object, or it is not visible to this caller."

# One message for a malformed identifier, again without the identifier in it.
INVALID_REF_DETAIL: Final[str] = "Malformed object identifier."


class ObjectRefError(ValueError):
    """A caller-supplied identifier that must never reach a lookup."""

    def __init__(self, reason: str) -> None:
        # `reason` is for the server log. It names the rule that failed and
        # never carries the offending value, so a log line cannot become the
        # place the raw identifier is stored unescaped.
        super().__init__(reason)
        self.reason = reason


def validate_object_id(value: object) -> str:
    """
    Return *value* unchanged if it is a well-formed identifier; raise otherwise.

    Raises
    ------
    ObjectRefError
        With a reason naming the rule, never quoting the input.
    """
    if not isinstance(value, str):
        raise ObjectRefError("identifier is not a string")
    if not value:
        raise ObjectRefError("identifier is empty")
    if len(value) > MAX_OBJECT_ID_LEN:
        raise ObjectRefError("identifier exceeds the maximum length")
    # Checked before the pattern so the log says which class of attack it was.
    # The pattern alone would reject all of these anyway; the distinction is
    # for the operator reading the alert, not for the control.
    if "\x00" in value:
        raise ObjectRefError("identifier contains a null byte")
    if "/" in value or "\\" in value:
        raise ObjectRefError("identifier contains a path separator")
    if ".." in value:
        raise ObjectRefError("identifier contains a traversal sequence")
    if not _OBJECT_ID_RE.fullmatch(value):
        raise ObjectRefError("identifier does not match the permitted character set")
    return value


def is_valid_object_id(value: object) -> bool:
    """Predicate form, for call sites that branch rather than raise."""
    try:
        validate_object_id(value)
    except ObjectRefError:
        return False
    return True


def assert_in_caller_scope(owner: str | None, caller: str) -> None:
    """
    Raise unless the resource's *owner* is the *caller*.

    An owner of ``None`` -- a resource with no recorded principal -- is
    **denied**, not allowed. Unattributed data is the case where a scoping
    bug is most likely to have happened, so it is the case that must fail
    closed rather than the case that gets the benefit of the doubt.
    """
    if owner is None:
        raise ObjectRefError("resource has no recorded owner")
    if owner != caller:
        raise ObjectRefError("resource belongs to another principal")


def not_found_response() -> dict[str, str]:
    """The one negative body, identical for every reason a lookup failed."""
    return {"error": NOT_FOUND_DETAIL}
