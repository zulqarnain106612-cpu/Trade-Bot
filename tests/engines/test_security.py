"""Tests for Part II security hardening modules."""

import pytest

from src.security.constant_time import safe_compare, safe_compare_bytes, safe_compare_tokens
from src.security.credential_vault import CredentialVault
from src.security.pq_transport import PQTransportStub

# ---------------------------------------------------------------------------
# Constant-time comparisons
# ---------------------------------------------------------------------------


def test_safe_compare_equal():
    assert safe_compare("hello", "hello") is True


def test_safe_compare_not_equal():
    assert safe_compare("hello", "world") is False


def test_safe_compare_bytes_equal():
    assert safe_compare_bytes(b"abc", b"abc") is True


def test_safe_compare_bytes_not_equal():
    assert safe_compare_bytes(b"abc", b"xyz") is False


def test_safe_compare_tokens_equal():
    assert safe_compare_tokens("token123", "token123") is True


def test_safe_compare_tokens_not_equal():
    assert safe_compare_tokens("token123", "other456") is False


# ---------------------------------------------------------------------------
# Api Signer (Ed25519)
# ---------------------------------------------------------------------------


def test_api_signer_sign_verify():
    from src.security.api_signer import ApiSigner

    signer = ApiSigner()
    sig = signer.sign_request("POST", "/api/order", '{"symbol":"BTC"}', 1234567890)
    assert isinstance(sig, str)
    assert len(sig) == 128  # 64 bytes Ed25519 sig → 128 hex chars
    assert signer.verify("POST", "/api/order", '{"symbol":"BTC"}', 1234567890, sig)


def test_api_signer_deterministic():
    from src.security.api_signer import ApiSigner

    signer = ApiSigner()
    sig1 = signer.sign_request("GET", "/ping", "", 42)
    sig2 = signer.sign_request("GET", "/ping", "", 42)
    assert sig1 == sig2


def test_api_signer_wrong_method_fails():
    from src.security.api_signer import ApiSigner

    signer = ApiSigner()
    sig = signer.sign_request("POST", "/api/order", "{}", 100)
    assert not signer.verify("GET", "/api/order", "{}", 100, sig)


def test_api_signer_public_key_b64():
    import base64

    from src.security.api_signer import ApiSigner

    signer = ApiSigner()
    pub = signer.public_key_b64()
    assert isinstance(pub, str)
    raw = base64.b64decode(pub)
    assert len(raw) == 32  # Ed25519 public key is 32 bytes


# ---------------------------------------------------------------------------
# Credential Vault (BIP-32)
# ---------------------------------------------------------------------------


def test_credential_vault_derives_deterministically():
    seed = b"a" * 32
    vault = CredentialVault(seed)
    k1 = vault.api_key_for("binance")
    k2 = vault.api_key_for("binance")
    assert k1 == k2


def test_credential_vault_different_exchanges_different_keys():
    seed = b"b" * 32
    vault = CredentialVault(seed)
    k_binance = vault.api_key_for("binance")
    k_okx = vault.api_key_for("okx")
    assert k_binance != k_okx


def test_credential_vault_different_indices_different_keys():
    seed = b"c" * 32
    vault = CredentialVault(seed)
    k0 = vault.api_key_for("binance", key_index=0)
    k1 = vault.api_key_for("binance", key_index=1)
    assert k0 != k1


def test_credential_vault_short_seed_raises():
    with pytest.raises(ValueError, match="Master seed"):
        CredentialVault(b"short")


def test_credential_vault_hardened_path():
    seed = b"d" * 32
    vault = CredentialVault(seed)
    derived = vault.derive("binance")
    # All components should be hardened (marked with ')
    assert "'" in derived.path
    parts = derived.path.split("/")[1:]  # skip 'm'
    assert all(p.endswith("'") for p in parts)


# ---------------------------------------------------------------------------
# PQ Transport Stub
# ---------------------------------------------------------------------------


def test_pq_transport_not_available_by_default():
    stub = PQTransportStub()
    assert not stub._AVAILABLE


def test_pq_transport_keygen_raises_when_unavailable():
    stub = PQTransportStub()
    with pytest.raises(RuntimeError, match="not yet wired"):
        stub.keygen()


def test_pq_transport_threat_flag_default_false():
    assert PQTransportStub.is_quantum_threat_imminent() is False
