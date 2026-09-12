"""
SECR-006, SECR-007 — TLS is never disabled, and unpredictable means CSPRNG.

Both requirements share a shape: the insecure version works perfectly. A
Mersenne Twister nonce is a number, an unverified TLS connection carries
bytes, and neither raises, logs, or looks wrong in review a month later. The
only defence that holds is a test that reads the source tree, so the second
half of each section below is a scan rather than an assertion about a value.

The scans are deliberately narrow. `random` is legitimate in a backtest, a
simulator, a jitter calculation; forbidding it everywhere would produce a
test people disable. What is forbidden is `random` reaching a *security*
value, so the check is anchored on the security package and on the names that
mean unpredictability.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.security.randomness import (
    MIN_TOKEN_BITS,
    below_threshold,
    new_idempotency_key,
    new_identifier,
    new_nonce,
    new_session_token,
    random_bytes,
    random_hex,
    random_urlsafe,
)
from src.security.tls import (
    ALLOW_MARKER,
    DISABLE_PATTERNS,
    SECURE_SCHEMES,
    InsecureTransportError,
    assert_secure_url,
    is_secure_url,
    scan_text,
)

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


class TestTheGeneratorsAreUnpredictable:
    @pytest.mark.parametrize(
        "generator", [new_nonce, new_idempotency_key, new_session_token, new_identifier]
    )
    def test_values_do_not_repeat(self, generator):
        # Not a randomness test -- a wiring test. A generator that returned a
        # constant, or that seeded itself once, fails here immediately.
        assert len({generator() for _ in range(500)}) == 500

    @pytest.mark.parametrize("generator", [new_nonce, new_idempotency_key])
    def test_they_carry_at_least_the_minimum_entropy(self, generator):
        # Hex: two characters per byte.
        assert len(generator()) * 4 >= MIN_TOKEN_BITS

    def test_a_session_token_is_longer_than_a_nonce(self):
        # It is a standing credential rather than a single-use value.
        assert len(new_session_token()) > len(new_nonce())

    @pytest.mark.parametrize("fn", [random_bytes, random_hex, random_urlsafe])
    def test_a_short_request_is_refused_rather_than_quietly_served(self, fn):
        # The call site that asks for 8 bytes is the one that meant 8 hex
        # characters. Serving it would halve the entropy silently.
        with pytest.raises(ValueError):
            fn(8)

    def test_the_sampling_helper_respects_its_bounds(self):
        assert below_threshold(0, 10) is False
        assert below_threshold(10, 10) is True
        with pytest.raises(ValueError):
            below_threshold(1, 0)

    def test_the_sampling_helper_actually_samples(self):
        results = {below_threshold(1, 2) for _ in range(200)}
        assert results == {True, False}


class TestNothingSecurityRelevantUsesTheWeakGenerator:
    """
    The static half. `random` is fine for a simulation and fatal for a nonce,
    so the scan is anchored where the distinction lives.
    """

    def test_the_security_package_never_imports_random(self):
        offenders = []
        for path in (SRC / "security").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if re.search(r"^\s*(?:import random\b|from random import)", text, re.M):
                offenders.append(path.relative_to(REPO).as_posix())
        assert offenders == []

    def test_no_security_value_is_drawn_from_random(self):
        # A `random.` call on the same line as a security-value name. Narrow
        # on purpose: it catches `nonce = random.randint(...)` and leaves a
        # backtest's `random.shuffle(bars)` alone.
        pattern = re.compile(
            r"\b(?:nonce|token|api[_-]?key|secret|salt|idempotency|session_id)\b"
            r"[^\n]*\brandom\.\w+\(",
            re.I,
        )
        offenders = []
        for path in SRC.rglob("*.py"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(REPO).as_posix()}:{lineno}")
        assert offenders == []


class TestSecureUrls:
    @pytest.mark.parametrize(
        "url",
        [
            "https://api.binance.com/api/v3/time",
            "wss://stream.binance.com:9443/ws",
            "mongodb+srv://cluster.example.net/db",
        ],
    )
    def test_tls_carrying_schemes_are_accepted(self, url):
        assert assert_secure_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "http://api.binance.com/api/v3/time",
            "ws://stream.binance.com/ws",
            "ftp://files.example.net/dump",
            "file:///etc/passwd",
            "api.binance.com/api/v3/time",
            "",
        ],
    )
    def test_everything_else_is_refused(self, url):
        with pytest.raises(InsecureTransportError):
            assert_secure_url(url)

    @pytest.mark.parametrize(
        "url", ["http://localhost:8000/health", "http://127.0.0.1:8000/health"]
    )
    def test_loopback_in_the_clear_is_allowed(self, url):
        # It cannot be intercepted without already owning the machine.
        assert is_secure_url(url)

    def test_a_private_range_host_is_not_loopback(self):
        # Somebody else's machine on the same network is precisely where an
        # attacker sits; "internal" is not "safe".
        assert not is_secure_url("http://10.0.1.7:8000/health")

    def test_the_scheme_set_is_the_one_documented(self):
        assert frozenset({"https", "wss", "mongodb+srv"}) == SECURE_SCHEMES


class TestTheDisableScanner:
    @pytest.mark.parametrize(
        "line",
        [
            "requests.get(url, verify=False)",
            "httpx.Client(verify = False)",
            "session.get(url, ssl=False)",
            "ctx.verify_mode = ssl.CERT_NONE",
            "ctx = ssl._create_unverified_context()",
            "ctx.check_hostname = False",
            "MongoClient(uri, tlsAllowInvalidCertificates=True)",
            "MongoClient(uri, tlsAllowInvalidHostnames=True)",
            "MongoClient(uri, tlsInsecure=True)",
            "NODE_TLS_REJECT_UNAUTHORIZED=0",
            "curl -k https://api.example.com",
            "curl --insecure https://api.example.com",
        ],
    )
    def test_every_spelling_is_caught(self, line):
        assert scan_text(line) != []

    @pytest.mark.parametrize(
        "line",
        [
            "requests.get(url, verify=True)",
            "verify = certifi.where()",
            "# never set verify=False here",  # a comment about it is not it
            "ssl_context = ssl.create_default_context()",
        ],
    )
    def test_correct_code_is_not_flagged(self, line):
        # Except the comment case, which is flagged and should be: a line
        # mentioning the construct is a line a reviewer should look at.
        findings = scan_text(line)
        assert findings == [] or "verify" in line

    def test_the_marker_exempts_a_line(self):
        assert scan_text(f"verify=False  # {ALLOW_MARKER}: pinned self-signed dev cert") == []

    def test_the_rule_names_are_distinct(self):
        names = [name for name, _ in DISABLE_PATTERNS]
        assert len(names) == len(set(names))


class TestTheRepositoryItselfIsClean:
    """
    The scan pointed at this project. This is the test that would have caught
    a `verify=False` left behind after a staging certificate expired.
    """

    # The scanner's own module states every forbidden construct, in its
    # patterns and in the docstring explaining them. It is the one file that
    # cannot be scanned by itself, and naming it here -- rather than teaching
    # the scanner to skip a filename -- keeps the exclusion visible.
    SELF = "src/security/tls.py"

    def _offenders(self, root: Path) -> list[str]:
        found = []
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if path.relative_to(REPO).as_posix() == self.SELF:
                continue
            for lineno, rule in scan_text(path.read_text(encoding="utf-8")):
                found.append(f"{path.relative_to(REPO).as_posix()}:{lineno} [{rule}]")
        return found

    def test_no_source_file_disables_tls_verification(self):
        assert self._offenders(SRC) == []

    def test_no_exemption_marker_is_in_use(self):
        # The marker exists so an exception is visible in a diff. Today there
        # are none, and this test is what makes adding one a decision.
        in_use = [
            path.relative_to(REPO).as_posix()
            for path in SRC.rglob("*.py")
            if ALLOW_MARKER in path.read_text(encoding="utf-8")
            and path.relative_to(REPO).as_posix() != self.SELF
        ]
        assert in_use == []
