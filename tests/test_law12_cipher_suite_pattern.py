"""
REG-0017 — LAW12 must flag the suites that lack forward secrecy, and only
those.

The rule read `DH(?:E|_anon)?\\b|DHE_RSA|TLS_RSA_WITH` under
``re.IGNORECASE``, which inverted it twice over. `DHE` and `ECDHE` are the
*ephemeral* exchanges that provide forward secrecy, so the recommended
suites were reported as "Non-PFS"; and with no left boundary and a
case-insensitive match, any word ending in "dh" -- a lowercase ``"ecdh"``
key in a lookup table, the word "ECDH" in a sentence -- read the same as a
configured cipher suite.

Both directions are pinned here because a regex is only as good as the
cases nobody re-derives: a later edit that restores the broad pattern
passes every other test in the repository, and the visible symptom is a
HIGH finding on prose.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

VALIDATOR = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "skills"
    / "crypto-architect"
    / "scripts"
    / "validate_arch.py"
)


@pytest.fixture(scope="module")
def law12_suite_pattern() -> re.Pattern[str]:
    """
    The compiled LAW12 cipher-suite pattern, read from the validator source.

    Read rather than imported: the validator is a skill script outside the
    package, and importing it for one constant pulls in its whole module.
    """
    tree = ast.parse(VALIDATOR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple) or len(node.elts) != 4:
            continue
        head = node.elts[0]
        message = node.elts[2]
        if (
            isinstance(head, ast.Constant)
            and head.value == "LAW12"
            and isinstance(message, ast.Constant)
            and "Non-PFS" in str(message.value)
        ):
            return re.compile(ast.literal_eval(node.elts[1]), re.IGNORECASE)
    pytest.fail("LAW12 cipher-suite rule not found in validate_arch.py")


@pytest.mark.parametrize(
    "suite",
    [
        "TLS_RSA_WITH_AES_128_CBC_SHA",  # RSA key transport, no PFS
        "TLS_DH_RSA_WITH_AES_128_CBC_SHA",  # static DH
        "TLS_DH_DSS_WITH_AES_256_CBC_SHA",  # static DH
        "TLS_ECDH_RSA_WITH_AES_128_GCM_SHA256",  # static ECDH
        "TLS_DH_anon_WITH_AES_256_CBC_SHA",  # unauthenticated
        "TLS_RSA_EXPORT_WITH_RC4_40_MD5",  # export grade
    ],
)
def test_suites_without_forward_secrecy_are_flagged(
    law12_suite_pattern: re.Pattern[str], suite: str
) -> None:
    assert law12_suite_pattern.search(suite), suite


@pytest.mark.parametrize(
    "text",
    [
        "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256",  # ephemeral: this is the goal
        "TLS_DHE_RSA_WITH_AES_256_GCM_SHA384",  # ephemeral
        'SCHEME_FAMILIES = {"ecdh": SchemeFamily.ELLIPTIC_CURVE_DLP}',
        'SCHEME_FAMILIES = {"ffdh": SchemeFamily.FINITE_FIELD_DLP}',
        'SCHEME_FAMILIES = {"dh": SchemeFamily.FINITE_FIELD_DLP}',
        "ECDSA and ECDH share a fate: both reduce to the same discrete log.",
    ],
)
def test_forward_secret_suites_and_prose_are_not_flagged(
    law12_suite_pattern: re.Pattern[str], text: str
) -> None:
    """
    A false HIGH is not a harmless one: it is either baselined -- and the
    suppression then hides the real finding that file later grows -- or it
    trains a reviewer to read LAW12 as noise.
    """
    assert not law12_suite_pattern.search(text), text
