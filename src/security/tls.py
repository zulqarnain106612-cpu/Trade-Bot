"""
SECR-006 — TLS is verified everywhere, and verification is never disabled.

`verify=False` is not a security decision, it is a debugging step somebody
forgot to undo. It appears when a staging certificate expires and the fix is
needed in ten minutes, and it survives because nothing fails afterwards: the
connection works, the data flows, and the only difference is that an attacker
on the path can now read and rewrite it. For a trading bot the traffic in
question is orders and balances.

The same applies to its relatives -- `ssl=False` on an aiohttp session,
`tlsAllowInvalidCertificates` on a MongoDB URI, `NODE_TLS_REJECT_UNAUTHORIZED=0`
in an environment file, `ssl._create_unverified_context()`. Each is a
different spelling of the same sentence.

This module gives the codebase two things: a checked way to state an outbound
URL is acceptable, and a scanner that the test suite points at the source tree
so a new spelling has to get past a test rather than past a reviewer.

post_quantum posture (LAW12):
  Relevant, but not this module's to fix. The TLS sessions it insists on are
  established with X25519 or ECDH key agreement today, and those are exactly
  what a cryptographically relevant quantum computer breaks -- with
  harvest-now-decrypt-later exposure, since a recorded session can be opened
  later. The replacement is hybrid ML-KEM key agreement (X25519MLKEM768),
  which is negotiated by the TLS stack and the peer, not chosen here: this
  module decides *whether* TLS is used, never which suite.

  What *would* change it: pinning a cipher suite or a TLS context in this
  project rather than accepting the library default. At that point the
  hybrid group belongs in the pinned configuration, and this posture becomes
  a real migration item instead of a dependency note.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlparse

# Schemes that carry TLS. A trading system has no business on the others.
SECURE_SCHEMES: Final[frozenset[str]] = frozenset({"https", "wss", "mongodb+srv"})

# Allowed in cleartext, because they never leave the machine. Localhost only:
# a private-range address is somebody else's machine on the same network,
# which is exactly the position an attacker wants.
LOCAL_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})

# Every way this codebase's dependency set can be told to skip verification.
# Spelled as patterns rather than substrings so `verify = False` with spaces,
# and the keyword-argument form, both match.
DISABLE_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("requests/httpx verify", re.compile(r"\bverify\s*=\s*False\b")),
    ("aiohttp ssl", re.compile(r"\bssl\s*=\s*False\b")),
    ("ssl verify_mode", re.compile(r"verify_mode\s*=\s*ssl\.CERT_NONE")),
    ("unverified context", re.compile(r"_create_unverified_context\s*\(")),
    ("check_hostname", re.compile(r"check_hostname\s*=\s*False")),
    ("mongo invalid certs", re.compile(r"tlsAllowInvalidCertificates\s*=\s*[Tt]rue")),
    ("mongo invalid hostnames", re.compile(r"tlsAllowInvalidHostnames\s*=\s*[Tt]rue")),
    ("mongo tls off", re.compile(r"tlsInsecure\s*=\s*[Tt]rue")),
    ("node tls off", re.compile(r"NODE_TLS_REJECT_UNAUTHORIZED\s*[=:]\s*['\"]?0")),
    ("curl insecure", re.compile(r"curl\b[^\n]*\s(?:-k|--insecure)\b")),
)

# A line carrying this marker is a deliberate, reviewed exception. It exists
# so the scanner has an escape hatch that is visible in a diff, rather than
# people switching the scanner off wholesale.
ALLOW_MARKER: Final[str] = "tls-verify-exempt"


class InsecureTransportError(RuntimeError):
    """An outbound destination that would travel unprotected."""


def assert_secure_url(url: str) -> str:
    """
    Return *url* if it will be carried over TLS; raise otherwise.

    Loopback over http is permitted because it cannot be intercepted without
    already owning the machine -- at which point TLS to yourself protects
    nothing. Every other cleartext destination is refused.
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in SECURE_SCHEMES:
        return url
    host = (parsed.hostname or "").lower()
    if scheme in {"http", "ws"} and host in LOCAL_HOSTS:
        return url
    raise InsecureTransportError(
        f"refusing a {scheme or 'scheme-less'} destination: TLS is required"
    )


def is_secure_url(url: str) -> bool:
    """Predicate form of `assert_secure_url`."""
    try:
        assert_secure_url(url)
    except InsecureTransportError:
        return False
    return True


def scan_text(text: str) -> list[tuple[int, str]]:
    """
    Find TLS-disabling constructs in *text*.

    Returns (line number, rule name) pairs. A line carrying `ALLOW_MARKER` is
    skipped -- and the test that consumes this asserts the exception list is
    empty, so an exception is a visible decision rather than a silent one.
    """
    findings: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for name, pattern in DISABLE_PATTERNS:
            if pattern.search(line):
                findings.append((lineno, name))
    return findings
