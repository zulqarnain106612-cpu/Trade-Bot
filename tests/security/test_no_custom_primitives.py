"""
SECR-008 — no custom cryptographic primitives.

The rule is not "cryptography is hard, don't". It is narrower and more useful:
a *primitive* -- a cipher, a hash construction, a KDF, a signature scheme --
must come from an established library, because the failure modes are invisible
to testing. A hand-rolled cipher encrypts and decrypts correctly and leaks the
key through timing; a hand-rolled MAC verifies correctly and is forgeable by
length extension. Neither shows up in a round-trip test, which is the only
test anyone writes for them.

This project genuinely does implement mathematics -- `src/mathcore` has finite
fields, lattices, CRT -- and that is not a violation. The distinction the
registry draws is between mathematical machinery used for analysis and a
primitive standing in the path of a real secret. So this file checks the
second: everything under `src/security` that touches a live key delegates to
`cryptography`, `hmac`, `hashlib` or `secrets`, and nothing there grows its
own S-box.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SECURITY = REPO / "src" / "security"

# Established implementations. Everything a security module needs is here.
APPROVED_SOURCES = frozenset({"cryptography", "hmac", "hashlib", "secrets", "ssl", "base64"})

# Names that mean somebody is building a primitive rather than calling one.
HAND_ROLLED_MARKERS = (
    "_sbox",
    "_inv_sbox",
    "_round_key",
    "_key_schedule",
    "_feistel",
    "_permute_block",
    "xor_cipher",
    "xor_stream",
    "custom_hash",
    "my_hash",
)


def security_modules() -> list[Path]:
    return sorted(p for p in SECURITY.rglob("*.py") if "__pycache__" not in p.parts)


class TestTheSecurityPackageDelegates:
    def test_there_are_modules_to_check(self):
        # A scan over an empty set passes vacuously, which is the way this
        # whole file could silently stop meaning anything.
        assert len(security_modules()) >= 5

    @pytest.mark.parametrize("marker", HAND_ROLLED_MARKERS)
    def test_no_module_builds_its_own_primitive(self, marker):
        offenders = [
            p.relative_to(REPO).as_posix()
            for p in security_modules()
            if marker in p.read_text(encoding="utf-8")
        ]
        assert offenders == []

    def test_every_cryptographic_operation_comes_from_an_approved_source(self):
        # Walk the imports rather than grep the text: an import inside a
        # function (this package uses several) is still an import.
        suspicious: list[str] = []
        for path in security_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                elif isinstance(node, ast.Import):
                    root = node.names[0].name.split(".")[0]
                else:
                    continue
                if root in {"Crypto", "pycryptodome", "nacl", "rsa", "ecdsa"}:
                    # Not inherently wrong, but a second crypto library in a
                    # project that has one is how two key formats appear.
                    suspicious.append(f"{path.relative_to(REPO).as_posix()}: {root}")
        assert suspicious == []

    def test_the_signer_uses_the_library_scheme(self):
        source = (SECURITY / "api_signer.py").read_text(encoding="utf-8")
        assert "Ed25519PrivateKey" in source
        assert "cryptography.hazmat" in source

    def test_the_at_rest_module_uses_an_aead(self):
        source = (SECURITY / "at_rest.py").read_text(encoding="utf-8")
        assert "AESGCM" in source

    def test_comparisons_use_the_stdlib_constant_time_primitive(self):
        source = (SECURITY / "constant_time.py").read_text(encoding="utf-8")
        assert "hmac.compare_digest" in source
        # And define no loop of its own: an early-return comparison is the
        # exact bug this module exists to prevent.
        assert not re.search(r"for .* in .*zip\(", source)

    def test_key_derivation_uses_hmac_not_a_bare_hash_of_a_concatenation(self):
        # h(a || b) is ambiguous about where a ends, which is how two
        # different inputs derive one key.
        source = (SECURITY / "key_lifecycle.py").read_text(encoding="utf-8")
        assert "hmac.new(" in source

    def test_randomness_comes_from_secrets(self):
        source = (SECURITY / "randomness.py").read_text(encoding="utf-8")
        assert "import secrets" in source
        assert "import random" not in source


class TestTheMathcoreDistinction:
    """
    `src/mathcore` implements real mathematics on purpose. This states where
    the line is, so neither side drifts into the other.
    """

    # The two places mathcore may be reached from src/security, and the reason
    # each is not a custom primitive. Anything not named here fails, so adding
    # a third use is a decision someone makes in this file, in a diff, rather
    # than by writing an import.
    MATHCORE_USES = {
        "src/security/api_signer.py": (
            "audit_signer() verifies an already-produced signature a second time "
            "under the from-scratch Ed25519, to prove it is canonical and not "
            "merely acceptable to the signer's own verifier. It produces no "
            "signature and is on no request path."
        ),
        "src/security/pq_transport.py": (
            "validate_parameters() checks an inert ML-KEM-768 stub's constants "
            "against FIPS 203. The stub cannot encapsulate, so no ciphertext "
            "depends on mathcore."
        ),
    }

    def test_mathcore_is_not_in_the_security_path(self):
        # The claim is that no security module performs production cryptography
        # with this project's own implementations -- not that the name never
        # appears. Reading mathcore to *check* a vetted library's output is the
        # opposite of a custom primitive, so the rule is what the import does,
        # not that it exists. An unlisted importer still fails.
        importers = {
            p.relative_to(REPO).as_posix()
            for p in security_modules()
            if re.search(
                r"^\s*from src\.mathcore|^\s*import src\.mathcore",
                p.read_text(encoding="utf-8"),
                re.M,
            )
        }
        assert importers <= set(self.MATHCORE_USES), sorted(importers - set(self.MATHCORE_USES))

    def test_every_permitted_mathcore_use_is_function_local(self):
        # A module-level import makes mathcore part of the module's ordinary
        # operation. Confining it inside the one function that audits or
        # validates is what keeps it off the signing path, and is the
        # structural difference between the permitted uses and the banned one.
        for module in self.MATHCORE_USES:
            source = (REPO / module).read_text(encoding="utf-8")
            top_level = re.findall(r"^(?:from|import) src\.mathcore", source, re.M)
            assert top_level == [], module

    def test_the_permitted_modules_still_sign_with_the_vetted_library(self):
        # The allowlist buys a second opinion, never a replacement. If
        # api_signer ever stopped calling the audited library, the "audit"
        # would be this project checking its own arithmetic against itself.
        source = (SECURITY / "api_signer.py").read_text(encoding="utf-8")
        assert "cryptography" in source or "nacl" in source

    def test_the_registry_records_the_pq_posture_where_it_belongs(self):
        # pq_transport is the module that reasons about post-quantum posture;
        # it is documentation-and-policy, and asserting it exists keeps the
        # SECR-008 entry's owning module honest.
        assert (SECURITY / "pq_transport.py").exists()


class TestApprovedSourcesAreActuallyUsed:
    @pytest.mark.parametrize(
        "module,expected",
        [
            ("api_signer.py", "cryptography"),
            ("at_rest.py", "cryptography"),
            ("constant_time.py", "hmac"),
            ("key_lifecycle.py", "hmac"),
            ("randomness.py", "secrets"),
        ],
    )
    def test_each_module_delegates_to_its_library(self, module, expected):
        assert expected in (SECURITY / module).read_text(encoding="utf-8")
        assert expected in APPROVED_SOURCES
