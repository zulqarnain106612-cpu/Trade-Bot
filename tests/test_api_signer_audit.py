"""
Tests for :func:`src.security.api_signer.audit_signer`.

The exit-gate clause is one sentence: audit_signer returns a clean report for
the current Ed25519 signer. That is the first test. The rest exist so a *clean*
result means something -- each check is shown to fail on a signer that violates
exactly the property it guards, because an audit that cannot go red is not an
audit.
"""

from __future__ import annotations

import pytest

from src.security.api_signer import ApiSigner, SignerAudit, audit_signer

pytest.importorskip("cryptography")

_REQUEST = ("POST", "/v1/audit", '{"probe":true}', 1_700_000_000)


def test_the_real_ed25519_signer_audits_clean() -> None:
    report = audit_signer(ApiSigner())
    assert report.clean
    assert report.algorithm == "Ed25519"
    assert report.findings == ()


def test_the_audit_confirms_determinism() -> None:
    """Ed25519's nonce is derived, not drawn; two signings must match."""
    report = audit_signer(ApiSigner())
    assert report.deterministic


def test_the_audit_cross_verifies_under_both_cofactor_conventions() -> None:
    report = audit_signer(ApiSigner())
    assert report.independent_verify_cofactorless
    assert report.independent_verify_cofactored


def test_a_randomised_nonce_signer_is_caught() -> None:
    """
    The property that matters most. A signer that re-randomises each signature
    is not deterministic, and the whole biased-nonce class reopens. The audit
    must go red, and its finding must name the reopened surface.
    """

    class RandomisedSigner(ApiSigner):
        def sign_request(self, method, path, body, timestamp):  # type: ignore[override]
            import os

            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )

            # Sign a *different* payload each time by salting it, so the output
            # changes across calls the way a randomised nonce would make it.
            salt = os.urandom(8).hex()
            payload = f"{timestamp}{method}{path}{body}{salt}".encode()
            del Ed25519PrivateKey
            return self._key.sign(payload).hex()

    report = audit_signer(RandomisedSigner())
    assert not report.clean
    assert not report.deterministic
    assert any("nonce" in f for f in report.findings)


def test_a_signer_that_cannot_verify_its_own_signature_is_caught() -> None:
    class BrokenVerifier(ApiSigner):
        def verify(self, method, path, body, timestamp, signature_hex):  # type: ignore[override]
            return False

    report = audit_signer(BrokenVerifier())
    assert not report.clean
    assert not report.round_trip_ok
    # A verifier that always says no still "rejects" the tampered signature, so
    # tamper_rejected stays True -- the round-trip failure is what catches this.
    assert report.tamper_rejected
    assert any("rejected its own signature" in f for f in report.findings)


def test_a_signer_that_accepts_anything_fails_tamper_detection() -> None:
    class AcceptAll(ApiSigner):
        def verify(self, method, path, body, timestamp, signature_hex):  # type: ignore[override]
            return True

    report = audit_signer(AcceptAll())
    assert not report.clean
    assert not report.tamper_rejected
    assert any("flipped byte" in f for f in report.findings)


def test_a_non_canonical_signature_fails_independent_verification() -> None:
    """
    A signer whose output the *independent* Ed25519 rejects is caught even if
    its own verifier is happy -- which is the reason the cross-check exists
    rather than trusting the signer to grade itself.
    """

    class Truncating(ApiSigner):
        def sign_request(self, method, path, body, timestamp):  # type: ignore[override]
            good = super().sign_request(method, path, body, timestamp)
            # Corrupt the S half so it is >= L: the independent verifier's
            # canonical-S check rejects it, though a naive verifier might not.
            raw = bytearray(bytes.fromhex(good))
            raw[63] |= 0xF0
            return bytes(raw).hex()

        def verify(self, method, path, body, timestamp, signature_hex):  # type: ignore[override]
            return True  # pretend it is fine

    report = audit_signer(Truncating())
    assert not report.clean
    assert not (report.independent_verify_cofactorless and report.independent_verify_cofactored)


def test_signer_audit_clean_requires_every_check() -> None:
    """The clean property is a strict AND -- no single green hides a red."""
    base = dict(
        algorithm="Ed25519",
        deterministic=True,
        round_trip_ok=True,
        independent_verify_cofactorless=True,
        independent_verify_cofactored=True,
        tamper_rejected=True,
        findings=(),
    )
    assert SignerAudit(**base).clean
    for flag in (
        "deterministic",
        "round_trip_ok",
        "independent_verify_cofactorless",
        "independent_verify_cofactored",
        "tamper_rejected",
    ):
        assert not SignerAudit(**{**base, flag: False}).clean
    assert not SignerAudit(**{**base, "findings": ("x",)}).clean


# ---- signer construction (kept covered here so the module stays fully
# exercised by this file alone, independent of other suites) ----------------


def test_a_signer_loaded_from_a_seed_audits_clean() -> None:
    """
    A signer built from a fixed base64 seed -- the production path -- signs
    deterministically and audits clean, and two signers from the same seed
    produce the same signature.
    """
    import base64

    seed = base64.b64encode(bytes(range(32))).decode()
    a = ApiSigner(seed)
    b = ApiSigner(seed)
    assert audit_signer(a).clean
    assert a.sign_request(*_REQUEST) == b.sign_request(*_REQUEST)


def test_from_env_uses_the_seed_when_present_and_an_ephemeral_key_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    seed = base64.b64encode(bytes(range(1, 33))).decode()
    monkeypatch.setenv("API_SIGNING_KEY_B64", seed)
    from_env = ApiSigner.from_env()
    direct = ApiSigner(seed)
    assert from_env.public_key_b64() == direct.public_key_b64()

    monkeypatch.delenv("API_SIGNING_KEY_B64", raising=False)
    ephemeral = ApiSigner.from_env()
    assert audit_signer(ephemeral).clean
