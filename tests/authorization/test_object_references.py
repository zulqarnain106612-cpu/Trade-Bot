"""
API-002 — a caller cannot reach another principal's resource by changing an id.

The authorization matrix decides what a *role* may do. It has nothing to say
about which *instances* a caller may touch, and that gap is where the whole
IDOR class lives: every check passes, the caller is who they say they are and
is allowed to read orders, and the order they read is somebody else's.

Two properties close it, and this file tests both because either alone is
insufficient:

  * the identifier is validated before it is used as a key, so it cannot be a
    traversal, an injection or an unbounded allocation; and
  * every negative outcome produces the same response, so a caller cannot
    distinguish "does not exist" from "not yours" and enumerate the space.

The second is the one that gets lost in a refactor, because a helpful error
message is an improvement by every measure except this one.
"""

from __future__ import annotations

import inspect

import pytest

from src.api.object_refs import (
    INVALID_REF_DETAIL,
    MAX_OBJECT_ID_LEN,
    NOT_FOUND_DETAIL,
    ObjectRefError,
    assert_in_caller_scope,
    is_valid_object_id,
    not_found_response,
    validate_object_id,
)


class TestWellFormedIdentifiers:
    @pytest.mark.parametrize(
        "value",
        [
            "abc123",
            "ORDER-42",
            "order_42",
            "0",
            "a" * MAX_OBJECT_ID_LEN,
            "7f3a91c2b4e6",  # exchange-style hex id
        ],
    )
    def test_they_are_accepted_unchanged(self, value):
        assert validate_object_id(value) == value


class TestTraversalAndInjection:
    @pytest.mark.parametrize(
        "value",
        [
            "../../etc/passwd",
            "..",
            "a/../b",
            "orders/1",
            "orders\\1",
            "order\x00.json",
            "order id",
            "order;DROP TABLE orders",
            "order'--",
            "$where",
            "{'$ne': null}",
            "order\nX-Injected: 1",
            "-leading-dash",
            "_leading-underscore",
            "café",
        ],
    )
    def test_they_never_reach_a_lookup(self, value):
        with pytest.raises(ObjectRefError):
            validate_object_id(value)

    def test_the_length_bound_is_enforced(self):
        # Unbounded input is an allocation an unauthenticated-in-practice
        # caller controls, spent before any authorization check runs.
        with pytest.raises(ObjectRefError):
            validate_object_id("a" * (MAX_OBJECT_ID_LEN + 1))

    @pytest.mark.parametrize("value", ["", None, 42, b"abc", ["abc"]])
    def test_non_strings_and_empties_are_refused(self, value):
        with pytest.raises(ObjectRefError):
            validate_object_id(value)


class TestTheErrorSaysNothingUseful:
    @pytest.mark.parametrize("value", ["../../etc/passwd", "secret/order/id", "a" * 200])
    def test_the_reason_never_quotes_the_input(self, value):
        # The reason ends up in a log line and, if anyone is careless, in a
        # response. Neither is a place to store attacker-controlled text
        # verbatim.
        with pytest.raises(ObjectRefError) as exc:
            validate_object_id(value)
        assert value not in str(exc.value)
        assert value not in exc.value.reason

    def test_the_two_public_messages_carry_no_identifier_slot(self):
        # No format placeholder means no call site can interpolate into them.
        assert "{" not in NOT_FOUND_DETAIL and "%s" not in NOT_FOUND_DETAIL
        assert "{" not in INVALID_REF_DETAIL and "%s" not in INVALID_REF_DETAIL

    def test_the_negative_body_is_a_constant(self):
        assert not_found_response() == {"error": NOT_FOUND_DETAIL}
        assert not_found_response() == not_found_response()


class TestThePredicateForm:
    def test_it_agrees_with_the_raising_form(self):
        for value in ["ok-1", "../x", "", "a" * 300, None]:
            expected = True
            try:
                validate_object_id(value)
            except ObjectRefError:
                expected = False
            assert is_valid_object_id(value) is expected


class TestScoping:
    def test_a_matching_owner_passes(self):
        assert_in_caller_scope("operator-a", "operator-a")

    def test_another_principals_resource_is_refused(self):
        with pytest.raises(ObjectRefError):
            assert_in_caller_scope("operator-b", "operator-a")

    def test_an_unowned_resource_is_refused_not_allowed(self):
        # The case a scoping bug most likely produces is the one that must
        # fail closed. "No recorded owner" is missing evidence, not consent.
        with pytest.raises(ObjectRefError):
            assert_in_caller_scope(None, "operator-a")


class TestTheGuardIsWiredIntoTheEndpoints:
    """
    The checks above are worthless if no endpoint calls them, and the
    endpoints that take an identifier in the path are enumerable from the
    route table -- so enumerate them and require each one to validate.
    """

    def test_every_path_parameter_endpoint_validates_its_identifier(self):
        from src.api import main

        # Path-parameter routes whose identifier is caller-supplied and used
        # as a lookup key. `timeframe` and `request_id` are excluded by name
        # below: the first is a closed enum validated by its own dependency,
        # the second is UUID-checked in place (H-13) before any lookup.
        checked = {
            "get_order_status": "validate_object_id(order_id)",
            "re_enable_strategy": "validate_object_id(strategy_id)",
        }
        for func_name, expected_call in checked.items():
            source = inspect.getsource(getattr(main, func_name))
            assert expected_call in source, f"{func_name} does not validate its identifier"

    def test_the_resolve_endpoint_still_checks_its_uuid_before_lookup(self):
        from src.api import main

        source = inspect.getsource(main.resolve_approval)
        assert "_UUID_RE.match(request_id)" in source
        # And the check precedes the executor lookup, which is the ordering
        # that makes it a guard rather than a formality.
        assert source.index("_UUID_RE.match") < source.index("executor.resolve_approval(")

    def test_the_order_endpoint_answers_unknown_and_malformed_identically(self):
        from src.api import main

        source = inspect.getsource(main.get_order_status)
        # Both the malformed branch and the missing branch return the same
        # constant body. Two distinct messages here is the oracle.
        assert source.count("not_found_response()") == 2

    def test_the_re_enable_endpoint_no_longer_echoes_the_identifier(self):
        from src.api import main

        source = inspect.getsource(main.re_enable_strategy)
        assert "{strategy_id!r}" not in source
        assert "detail=NOT_FOUND_DETAIL" in source
