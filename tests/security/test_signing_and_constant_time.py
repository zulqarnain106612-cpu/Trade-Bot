"""
SECR-002, SECR-005 — constant-time comparison, and signatures that expire.

Two requirements that fail in the same direction: the insecure version is
*correct*. `a == b` gives the right answer about equality; it just takes a
different amount of time depending on how much of the prefix matched, and
that difference is enough to recover a token a byte at a time over enough
requests. A signature verifies correctly forever; it just does not say
*when* it was made, so a captured "close all positions" works again tomorrow.

Timing is not asserted by measuring a clock -- a wall-clock assertion in CI is
a flake generator, and it would measure the test runner as much as the
comparison. What is asserted is that the code calls a constant-time
primitive, which is the property that actually holds, checked structurally
across the whole source tree.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from src.security.api_signer import (
    MAX_TIMESTAMP_SKEW_S,
    ApiSigner,
    ReplayError,
    ReplayWindow,
    verify_fresh_request,
)
from src.security.constant_time import safe_compare, safe_compare_bytes, safe_compare_tokens

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


class FakeClock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def signer() -> ApiSigner:
    return ApiSigner()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def window(clock) -> ReplayWindow:
    return ReplayWindow(now=clock)


class TestConstantTimeComparison:
    def test_equal_values_compare_equal(self):
        assert safe_compare("token-value", "token-value")
        assert safe_compare_bytes(b"\x00\x01", b"\x00\x01")
        assert safe_compare_tokens("abc", "abc")

    @pytest.mark.parametrize(
        "a,b",
        [
            ("token-value", "token-valuf"),  # differs in the last byte
            ("token-value", "xoken-value"),  # differs in the first
            ("token-value", "token-valu"),  # differs in length
            ("", "x"),
            ("x", ""),
        ],
    )
    def test_unequal_values_compare_unequal(self, a, b):
        assert not safe_compare(a, b)

    def test_a_long_shared_prefix_is_still_unequal(self):
        # The case a timing attack is built on: an attacker who has guessed
        # 31 of 32 bytes must learn nothing from the answer *shape*.
        base = "f" * 31
        assert not safe_compare(base + "a", base + "b")

    def test_unicode_is_handled_without_raising(self):
        assert not safe_compare("café", "cafe")


class TestNothingComparesSecretsWithEquals:
    """
    The structural half of SECR-002, over the whole source tree.
    """

    def test_no_secret_is_compared_with_a_plain_operator(self):
        # `if provided_key == stored_key:` and its relatives. The names are
        # what make a comparison security-relevant; comparing two symbols or
        # two timeframes with == is fine and stays fine.
        pattern = re.compile(
            r"\b(?:api[_-]?key|secret|token|signature|password|digest|hmac)\w*\s*[!=]=\s*"
            r"(?!None\b)",
            re.I,
        )
        offenders: list[str] = []
        for path in SRC.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(REPO).as_posix()}:{lineno}")
        assert offenders == []

    def test_the_helpers_delegate_to_hmac_compare_digest(self):
        import inspect

        from src.security import constant_time

        source = inspect.getsource(constant_time)
        assert source.count("hmac.compare_digest") >= 3


class TestSigning:
    def test_a_correct_signature_verifies(self, signer):
        ts = int(time.time())
        sig = signer.sign_request("POST", "/orders", '{"qty":1}', ts)
        assert signer.verify("POST", "/orders", '{"qty":1}', ts, sig)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("method", "GET"),
            ("path", "/orders/other"),
            ("body", '{"qty":2}'),
            ("timestamp", 1),
        ],
    )
    def test_tampering_with_any_signed_field_breaks_it(self, signer, field, value):
        args = {
            "method": "POST",
            "path": "/orders",
            "body": '{"qty":1}',
            "timestamp": int(time.time()),
        }
        sig = signer.sign_request(**args)
        args[field] = value
        assert not signer.verify(signature_hex=sig, **args)

    def test_a_forged_signature_is_refused(self, signer):
        ts = int(time.time())
        assert not signer.verify("POST", "/orders", "{}", ts, "00" * 64)

    def test_a_malformed_signature_is_refused_not_raised(self, signer):
        ts = int(time.time())
        assert not signer.verify("POST", "/orders", "{}", ts, "not-hex")

    def test_another_keys_signature_is_refused(self, signer):
        ts = int(time.time())
        sig = ApiSigner().sign_request("POST", "/orders", "{}", ts)
        assert not signer.verify("POST", "/orders", "{}", ts, sig)

    def test_signing_is_deterministic(self, signer):
        # Ed25519 derives its nonce from the key and message, so there is no
        # entropy failure to have. Asserted because it is the reason this
        # scheme was chosen over ECDSA.
        ts = int(time.time())
        a = signer.sign_request("POST", "/orders", "{}", ts)
        b = signer.sign_request("POST", "/orders", "{}", ts)
        assert a == b


class TestReplayAndFreshness:
    def _signed(self, signer, clock, path="/orders"):
        ts = int(clock.t)
        return ts, signer.sign_request("POST", path, "{}", ts)

    def test_a_fresh_request_is_accepted(self, signer, clock, window):
        ts, sig = self._signed(signer, clock)
        verify_fresh_request(signer, window, "POST", "/orders", "{}", ts, sig)

    def test_the_same_request_twice_is_refused(self, signer, clock, window):
        ts, sig = self._signed(signer, clock)
        verify_fresh_request(signer, window, "POST", "/orders", "{}", ts, sig)
        with pytest.raises(ReplayError, match="already"):
            verify_fresh_request(signer, window, "POST", "/orders", "{}", ts, sig)

    def test_a_stale_request_is_refused(self, signer, clock, window):
        ts, sig = self._signed(signer, clock)
        clock.t += MAX_TIMESTAMP_SKEW_S + 1
        with pytest.raises(ReplayError, match="old"):
            verify_fresh_request(signer, window, "POST", "/orders", "{}", ts, sig)

    def test_a_post_dated_request_is_refused(self, signer, clock, window):
        # Otherwise an attacker mints a signature that stays replayable for
        # as long as they care to post-date it.
        future = int(clock.t + MAX_TIMESTAMP_SKEW_S + 5)
        sig = signer.sign_request("POST", "/orders", "{}", future)
        with pytest.raises(ReplayError, match="future"):
            verify_fresh_request(signer, window, "POST", "/orders", "{}", future, sig)

    def test_a_request_just_inside_the_window_is_accepted(self, signer, clock, window):
        ts, sig = self._signed(signer, clock)
        clock.t += MAX_TIMESTAMP_SKEW_S - 1
        verify_fresh_request(signer, window, "POST", "/orders", "{}", ts, sig)

    def test_an_unverifiable_signature_never_reaches_the_cache(self, signer, clock, window):
        # The cheap check is the one an attacker controls the volume of, so a
        # garbage signature must not be able to occupy a cache slot.
        with pytest.raises(ReplayError, match="verify"):
            verify_fresh_request(signer, window, "POST", "/orders", "{}", int(clock.t), "00" * 64)
        assert window._seen == {}

    def test_the_cache_forgets_signatures_that_can_no_longer_verify(self, signer, clock, window):
        ts, sig = self._signed(signer, clock)
        verify_fresh_request(signer, window, "POST", "/orders", "{}", ts, sig)
        clock.t += MAX_TIMESTAMP_SKEW_S * 3
        ts2, sig2 = self._signed(signer, clock)
        verify_fresh_request(signer, window, "POST", "/orders", "{}", ts2, sig2)
        # The first entry has aged out -- and could not be replayed anyway,
        # because the freshness check would reject it first.
        assert sig not in window._seen

    def test_two_distinct_requests_at_one_timestamp_both_pass(self, signer, clock, window):
        ts, sig_a = self._signed(signer, clock, "/orders")
        _, sig_b = self._signed(signer, clock, "/positions")
        verify_fresh_request(signer, window, "POST", "/orders", "{}", ts, sig_a)
        verify_fresh_request(signer, window, "POST", "/positions", "{}", ts, sig_b)
